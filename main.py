import argparse
import asyncio
import json
import logging
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import JSONResponse, Response, RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware

import config
from core.client_loader import load_clients
from db.database import init_db, update_rating, set_human_takeover, get_weekly_stats
from webhooks.instantly_webhook import handle_instantly_webhook
from cron.outcome_tracker import run_outcome_tracker
from cron.weekly_digest import run_weekly_digest
from cron.confidence_calibrator import run_confidence_calibrator

logging.basicConfig(
    level=logging.INFO,
    format="[%(asctime)s] %(levelname)s %(name)s: %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("gleadsy-reply-agent")

# Global state
db = None
clients = {}
scheduler = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    global db, clients, scheduler

    # Init DB
    db = await init_db(config.DB_PATH)
    logger.info("DB initialized")

    # Restore confidence threshold from DB (persists across restarts)
    cursor = await db.execute(
        "SELECT new_threshold FROM confidence_log ORDER BY created_at DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    if row:
        config.CONFIDENCE_THRESHOLD = row["new_threshold"]
        logger.info(f"Confidence threshold restored: {config.CONFIDENCE_THRESHOLD}")

    # Load clients
    clients = load_clients(config.CLIENTS_DIR)
    logger.info(f"Klientai: {', '.join(clients.keys())}")

    # Start scheduler
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    scheduler = AsyncIOScheduler(timezone=config.TIMEZONE)

    scheduler.add_job(run_outcome_tracker, "interval", hours=6, args=[db], id="outcome_tracker")
    scheduler.add_job(run_weekly_digest, "cron", day_of_week="mon", hour=9, args=[db], id="weekly_digest")
    scheduler.add_job(run_confidence_calibrator, "cron", day_of_week="sun", hour=23, args=[db], id="confidence_calibrator")

    # Polling mode
    if config.REPLY_SOURCE == "polling":
        from core.instantly_client import poll_for_replies
        from datetime import timedelta

        # Check DB for last processed timestamp — avoid reprocessing old replies
        cursor = await db.execute("SELECT MAX(created_at) FROM interactions")
        last_row = await cursor.fetchone()
        if last_row and last_row[0]:
            # DB has data — start from last processed time
            last_poll = {"timestamp": last_row[0]}
            logger.info(f"Resuming from last processed: {last_row[0]}")
        else:
            # Fresh DB — start from NOW (don't reprocess old replies)
            last_poll = {"timestamp": datetime.utcnow().isoformat()}
            logger.info("Fresh start — polling only new replies from now")

        async def poll_job():
            replies = await poll_for_replies(last_poll["timestamp"])
            last_poll["timestamp"] = datetime.utcnow().isoformat()
            for reply in replies:
                await handle_instantly_webhook(reply, db, clients, config.CONFIDENCE_THRESHOLD)

        scheduler.add_job(poll_job, "interval", seconds=config.POLLING_INTERVAL_SECONDS, id="poller")
        logger.info(f"Polling mode: kas {config.POLLING_INTERVAL_SECONDS}s")

    # Periodic health monitoring — check API keys, DB, notify Slack on issues
    async def health_monitor():
        from core.slack_notifier import notify_error
        issues = []

        # Check DB
        try:
            await db.execute("SELECT 1")
        except Exception as e:
            issues.append(f"DB error: {e}")

        # Check Anthropic API key validity (lightweight — just verify key format)
        if not config.ANTHROPIC_API_KEY:
            issues.append("ANTHROPIC_API_KEY is empty!")
        elif not config.ANTHROPIC_API_KEY.startswith("sk-ant-"):
            issues.append("ANTHROPIC_API_KEY has invalid format")

        if not config.INSTANTLY_API_KEY:
            issues.append("INSTANTLY_API_KEY is empty!")

        if issues:
            await notify_error("health_check_failed", " | ".join(issues))
            logger.error(f"Health check failed: {issues}")

    scheduler.add_job(health_monitor, "interval", hours=1, id="health_monitor")

    scheduler.start()

    # Startup warnings
    if not config.WEBHOOK_SECRET:
        logger.warning("⚠️  WEBHOOK_SECRET not set — webhook endpoint is UNPROTECTED!")
    if not config.DASHBOARD_PASSWORD:
        logger.warning("⚠️  DASHBOARD_PASSWORD not set — dashboard is UNPROTECTED!")
    if not config.ANTHROPIC_API_KEY:
        logger.error("❌ ANTHROPIC_API_KEY not set — system cannot function!")
    if not config.INSTANTLY_API_KEY:
        logger.error("❌ INSTANTLY_API_KEY not set — cannot send replies!")

    logger.info(f"Gleadsy Reply Agent paleistas (port 8000)")
    logger.info(f"Webhook endpoint: POST /webhook/instantly")
    logger.info(f"Laukiu reply'ų...")

    yield

    scheduler.shutdown()
    if db:
        await db.close()


app = FastAPI(title="Gleadsy Reply Agent", lifespan=lifespan)


# --- Auth helpers ---

def _verify_webhook_secret(request: Request):
    """Verify webhook request has valid secret token."""
    if not config.WEBHOOK_SECRET:
        logger.warning("WEBHOOK_SECRET not set — webhook endpoint is unprotected!")
        return
    # Check header first, then query param
    token = request.headers.get("X-Webhook-Secret") or request.query_params.get("secret")
    if not token or not secrets.compare_digest(token, config.WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret")


def _get_dashboard_session(request: Request) -> bool:
    """Check if user has valid dashboard session cookie."""
    if not config.DASHBOARD_PASSWORD:
        return True  # No password set — open access
    session = request.cookies.get("gleadsy_session")
    if not session or not secrets.compare_digest(session, _session_token):
        return False
    return True


# Session token — generated on startup, lives in memory
_session_token = secrets.token_urlsafe(32)


@app.post("/webhook/instantly")
async def webhook_instantly(request: Request):
    _verify_webhook_secret(request)
    payload = await request.json()
    result = await handle_instantly_webhook(payload, db, clients, config.CONFIDENCE_THRESHOLD)
    return JSONResponse(content=result)


@app.post("/webhook/slack")
async def webhook_slack(request: Request):
    # Placeholder for future Slack interactive components
    return JSONResponse(content={"status": "not_implemented"})


@app.get("/health")
async def health():
    """Enhanced health check — verifies DB connectivity and API key presence."""
    health_status = {
        "status": "ok",
        "clients": list(clients.keys()),
        "confidence_threshold": config.CONFIDENCE_THRESHOLD,
        "checks": {},
    }
    all_ok = True

    # DB check
    try:
        cursor = await db.execute("SELECT COUNT(*) FROM interactions")
        count = (await cursor.fetchone())[0]
        health_status["checks"]["database"] = {"status": "ok", "interactions": count}
    except Exception as e:
        health_status["checks"]["database"] = {"status": "error", "error": str(e)}
        all_ok = False

    # API key check
    health_status["checks"]["anthropic_api_key"] = {"status": "ok" if config.ANTHROPIC_API_KEY else "missing"}
    if not config.ANTHROPIC_API_KEY:
        all_ok = False

    health_status["checks"]["instantly_api_key"] = {"status": "ok" if config.INSTANTLY_API_KEY else "missing"}
    if not config.INSTANTLY_API_KEY:
        all_ok = False

    # Slack check
    health_status["checks"]["slack_webhook"] = {"status": "ok" if config.SLACK_WEBHOOK_URL else "not_configured"}

    # Security checks
    health_status["checks"]["webhook_auth"] = {"status": "ok" if config.WEBHOOK_SECRET else "WARNING_unprotected"}
    health_status["checks"]["dashboard_auth"] = {"status": "ok" if config.DASHBOARD_PASSWORD else "WARNING_unprotected"}

    if not all_ok:
        health_status["status"] = "degraded"

    return health_status


@app.get("/login")
async def login_page(request: Request):
    """Show login form for dashboard access."""
    from fastapi.responses import HTMLResponse
    if not config.DASHBOARD_PASSWORD or _get_dashboard_session(request):
        return RedirectResponse(url="/replies", status_code=302)
    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Gleadsy - Login</title>
<style>
body { font-family: -apple-system, sans-serif; background: #f5f5f5; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
.card { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); width: 320px; text-align: center; }
h2 { color: #333; margin-bottom: 24px; }
input[type=password] { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; margin-bottom: 16px; box-sizing: border-box; }
button { width: 100%; padding: 12px; background: #4285f4; color: white; border: none; border-radius: 8px; font-size: 16px; cursor: pointer; font-weight: bold; }
button:hover { background: #3367d6; }
.error { color: #c62828; font-size: 13px; margin-top: 8px; display: none; }
</style></head><body>
<div class="card">
    <h2>Gleadsy Reply Agent</h2>
    <form method="POST" action="/login">
        <input type="password" name="password" placeholder="Slaptazodis" autofocus required>
        <button type="submit">Prisijungti</button>
    </form>
</div>
</body></html>"""
    return HTMLResponse(content=html)


@app.post("/login")
async def login_submit(request: Request):
    """Process login form."""
    from fastapi.responses import HTMLResponse
    form = await request.form()
    password = form.get("password", "")
    if config.DASHBOARD_PASSWORD and secrets.compare_digest(password, config.DASHBOARD_PASSWORD):
        response = RedirectResponse(url="/replies", status_code=302)
        response.set_cookie("gleadsy_session", _session_token, httponly=True, samesite="lax", max_age=86400 * 7)
        return response
    # Wrong password — show login with error
    html = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Gleadsy - Login</title>
<style>
body { font-family: -apple-system, sans-serif; background: #f5f5f5; display: flex; justify-content: center; align-items: center; min-height: 100vh; margin: 0; }
.card { background: white; padding: 40px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); width: 320px; text-align: center; }
h2 { color: #333; margin-bottom: 24px; }
input[type=password] { width: 100%; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; margin-bottom: 16px; box-sizing: border-box; }
button { width: 100%; padding: 12px; background: #4285f4; color: white; border: none; border-radius: 8px; font-size: 16px; cursor: pointer; font-weight: bold; }
button:hover { background: #3367d6; }
.error { color: #c62828; font-size: 13px; margin-top: 8px; }
</style></head><body>
<div class="card">
    <h2>Gleadsy Reply Agent</h2>
    <form method="POST" action="/login">
        <input type="password" name="password" placeholder="Slaptazodis" autofocus required>
        <button type="submit">Prisijungti</button>
        <p class="error">Neteisingas slaptazodis</p>
    </form>
</div>
</body></html>"""
    return HTMLResponse(content=html, status_code=401)


@app.get("/replies")
async def replies_dashboard(request: Request):
    """Web dashboard showing all replies from DB with rating buttons and quality scores."""
    from fastapi.responses import HTMLResponse
    import html as html_mod

    # Auth check
    if not _get_dashboard_session(request):
        return RedirectResponse(url="/login", status_code=302)

    cursor = await db.execute(
        "SELECT id, created_at, client_id, lead_email, classification, confidence, "
        "prospect_message, agent_reply, was_sent, human_rating, quality_score, quality_summary "
        "FROM interactions ORDER BY created_at DESC LIMIT 200"
    )
    rows = [dict(r) for r in await cursor.fetchall()]

    rows_html = ""
    for r in rows:
        iid = r["id"]
        orig = html_mod.escape(r.get("prospect_message") or "")
        reply = html_mod.escape(r.get("agent_reply") or "")
        classification = r.get("classification", "")
        confidence = r.get("confidence", 0)
        quality_score = r.get("quality_score")
        quality_summary = html_mod.escape(r.get("quality_summary") or "")
        rating = r.get("human_rating") or ""

        # Classification badge color
        cls_colors = {
            "INTERESTED": "#2e7d32", "QUESTION": "#1565c0", "NOT_NOW": "#e65100",
            "REFERRAL": "#6a1b9a", "UNSUBSCRIBE": "#c62828", "OUT_OF_OFFICE": "#757575",
            "UNCERTAIN": "#f9a825",
        }
        cls_color = cls_colors.get(classification, "#333")

        # Quality badge
        if quality_score is not None:
            if quality_score >= 8:
                q_color, q_bg = "#2e7d32", "#e8f5e9"
            elif quality_score >= 6:
                q_color, q_bg = "#e65100", "#fff3e0"
            else:
                q_color, q_bg = "#c62828", "#ffebee"
            quality_badge = f'<span class="badge" style="color:{q_color};background:{q_bg}" title="{quality_summary}">{quality_score}/10</span>'
        else:
            quality_badge = '<span class="badge" style="color:#999;background:#f5f5f5">-</span>'

        # Rating buttons or result
        if rating == "thumbs_up":
            rating_html = '<span style="font-size:20px">&#128077;</span>'
        elif rating == "thumbs_down":
            rating_html = '<span style="font-size:20px">&#128078;</span>'
        else:
            rating_html = f'''<button class="rate-btn up" onclick="rate({iid},'thumbs_up')" title="Geras atsakymas">&#128077;</button>
                <button class="rate-btn down" onclick="rate({iid},'thumbs_down')" title="Blogas atsakymas">&#128078;</button>'''

        sent_icon = "&#9989;" if r.get("was_sent") else "&#128221;"

        rows_html += f"""<tr id="row-{iid}">
            <td>{html_mod.escape(str(r.get('created_at','')))}</td>
            <td>{html_mod.escape(r.get('client_id',''))}</td>
            <td>{html_mod.escape(r.get('lead_email',''))}</td>
            <td><span class="badge" style="color:white;background:{cls_color}">{classification}</span></td>
            <td>{confidence:.0%}</td>
            <td>{quality_badge}</td>
            <td class="msg-col">{orig}</td>
            <td class="msg-col">{reply}</td>
            <td style="text-align:center">{sent_icon}</td>
            <td style="text-align:center;white-space:nowrap">{rating_html}</td>
        </tr>"""

    total = len(rows)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Gleadsy Reply Agent - Dashboard</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
h1 {{ color: #333; margin-bottom: 5px; }}
table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }}
th {{ background: #4285f4; color: white; padding: 12px 8px; text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; position: sticky; top: 0; }}
td {{ padding: 10px 8px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }}
tr:hover {{ background: #f0f7ff; }}
.badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
.count {{ color: #666; font-size: 14px; margin: 8px 0 16px; }}
.msg-col {{ max-width: 350px; white-space: pre-wrap; word-break: break-word; }}
.rate-btn {{ border: none; background: none; font-size: 18px; cursor: pointer; padding: 4px 6px; border-radius: 6px; transition: background 0.2s; }}
.rate-btn:hover {{ background: #e3f2fd; }}
.rate-btn.up:hover {{ background: #e8f5e9; }}
.rate-btn.down:hover {{ background: #ffebee; }}
.rated {{ animation: flash 0.5s; }}
@keyframes flash {{ 0%,100% {{ background: inherit; }} 50% {{ background: #e8f5e9; }} }}
.stats {{ display: flex; gap: 20px; margin-bottom: 16px; flex-wrap: wrap; }}
.stat-card {{ background: white; padding: 16px 24px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
.stat-card .number {{ font-size: 28px; font-weight: 700; color: #333; }}
.stat-card .label {{ font-size: 12px; color: #888; text-transform: uppercase; }}
</style></head><body>
<h1>Gleadsy Reply Agent</h1>
<p class="count">Auto-refresh kas 30s | TEST_MODE=true</p>

<div class="stats">
    <div class="stat-card"><div class="number">{total}</div><div class="label">Reply'ai</div></div>
</div>

<table>
<tr><th>Laikas</th><th>Klientas</th><th>Lead</th><th>Kategorija</th><th>Conf.</th><th>Quality</th><th>Lead zinute</th><th>Atsakymas</th><th>Sent</th><th>Vertinimas</th></tr>
{rows_html}
</table>

<script>
async function rate(id, rating) {{
    const row = document.getElementById('row-' + id);
    try {{
        const res = await fetch('/api/rate/' + id, {{
            method: 'POST',
            headers: {{'Content-Type': 'application/json'}},
            body: JSON.stringify({{rating: rating}})
        }});
        if (res.ok) {{
            const cell = row.querySelector('td:last-child');
            cell.innerHTML = rating === 'thumbs_up' ? '<span style="font-size:20px">&#128077;</span>' : '<span style="font-size:20px">&#128078;</span>';
            row.classList.add('rated');
            setTimeout(() => row.classList.remove('rated'), 600);
        }}
    }} catch(e) {{ console.error('Rating failed:', e); }}
}}
</script>
</body></html>"""
    return HTMLResponse(content=html)


@app.get("/answer/{interaction_id}")
async def answer_form(interaction_id: int, request: Request):
    """Show form to answer an unknown question."""
    from fastapi.responses import HTMLResponse
    if not _get_dashboard_session(request):
        return RedirectResponse(url="/login", status_code=302)
    cursor = await db.execute(
        "SELECT lead_email, client_id, prospect_message, classification FROM interactions WHERE id = ?",
        (interaction_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return HTMLResponse("<h2>Interaction not found</h2>", status_code=404)

    import html as html_mod
    lead_email = html_mod.escape(row[0])
    client_id = html_mod.escape(row[1])
    question = html_mod.escape(row[2])

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Atsakyti į klausimą</title>
<style>
body {{ font-family: -apple-system, sans-serif; margin: 20px; max-width: 700px; margin: 40px auto; background: #f5f5f5; }}
.card {{ background: white; padding: 30px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); }}
h2 {{ color: #1565c0; }}
.question {{ background: #f5f5f5; padding: 15px; border-radius: 8px; border-left: 4px solid #1565c0; white-space: pre-wrap; margin: 15px 0; }}
label {{ font-weight: bold; color: #333; display: block; margin-top: 20px; }}
textarea {{ width: 100%; height: 120px; padding: 12px; border: 1px solid #ddd; border-radius: 8px; font-size: 14px; font-family: inherit; resize: vertical; }}
button {{ margin-top: 15px; padding: 12px 30px; background: #1565c0; color: white; border: none; border-radius: 8px; font-size: 16px; cursor: pointer; font-weight: bold; }}
button:hover {{ background: #0d47a1; }}
.info {{ color: #666; font-size: 13px; }}
</style></head><body>
<div class="card">
    <h2>❓ Nežinomas klausimas</h2>
    <p><strong>Lead:</strong> {lead_email} | <strong>Klientas:</strong> {client_id}</p>
    <div class="question">{question}</div>
    <form method="POST" action="/answer/{interaction_id}">
        <label>Tavo atsakymas (bus pridėtas į FAQ):</label>
        <textarea name="answer" required placeholder="Rašyk atsakymą čia..."></textarea>
        <p class="info">Šis atsakymas bus išsaugotas kliento FAQ ir kitą kartą AI jau žinos ką atsakyti.</p>
        <button type="submit">Išsaugoti į FAQ</button>
    </form>
</div>
</body></html>"""
    return HTMLResponse(content=html)


@app.post("/answer/{interaction_id}")
async def answer_submit(interaction_id: int, request: Request):
    """Process human answer: save to FAQ YAML."""
    from fastapi.responses import HTMLResponse
    import yaml

    if not _get_dashboard_session(request):
        raise HTTPException(status_code=401, detail="Unauthorized")

    form = await request.form()
    answer_text = form.get("answer", "").strip()
    if not answer_text:
        return HTMLResponse("<h2>Tuščias atsakymas</h2>", status_code=400)

    cursor = await db.execute(
        "SELECT lead_email, client_id, prospect_message, email_id, campaign_id, email_account FROM interactions WHERE id = ?",
        (interaction_id,)
    )
    row = await cursor.fetchone()
    if not row:
        return HTMLResponse("<h2>Interaction not found</h2>", status_code=404)

    lead_email, client_id, question, email_id, campaign_id, email_account = row

    # 1. Add to client YAML FAQ
    yaml_path = config.CLIENTS_DIR / f"{client_id}.yaml"
    if yaml_path.exists():
        with open(yaml_path, "r", encoding="utf-8") as f:
            client_data = yaml.safe_load(f)
        if "faq" not in client_data:
            client_data["faq"] = []
        # Summarize question for FAQ
        short_question = question[:200].split("\n")[0].strip()
        client_data["faq"].append({
            "question": short_question,
            "answer": answer_text,
        })
        with open(yaml_path, "w", encoding="utf-8") as f:
            yaml.dump(client_data, f, allow_unicode=True, default_flow_style=False, sort_keys=False)
        # Reload clients
        from core.client_loader import load_all_clients
        global clients
        clients = load_all_clients(config.CLIENTS_DIR)
        logger.info(f"FAQ updated for {client_id}: '{short_question}' -> '{answer_text[:80]}...'")

    # 2. Update interaction with answer
    await db.execute(
        "UPDATE interactions SET agent_reply = ?, classification_reasoning = 'human_answered_faq' WHERE id = ?",
        (answer_text, interaction_id)
    )
    await db.commit()

    # 3. Update CSV if exists
    if config.TEST_MODE:
        from core.sheets_logger import log_test_reply
        log_test_reply(
            campaign_name="", client_id=client_id, lead_email=lead_email,
            company="", original_message=question, classification="QUESTION",
            confidence=1.0, generated_reply=answer_text,
            sending_account=email_account or "", status="human_answered_faq",
        )

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Atsakymas išsaugotas</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 700px; margin: 40px auto; background: #f5f5f5; }}
.card {{ background: white; padding: 30px; border-radius: 12px; box-shadow: 0 2px 8px rgba(0,0,0,0.1); text-align: center; }}
h2 {{ color: #2e7d32; }}
</style></head><body>
<div class="card">
    <h2>✅ Atsakymas išsaugotas!</h2>
    <p>FAQ atnaujintas klientui <strong>{client_id}</strong></p>
    <p>Kitą kartą AI jau žinos atsakymą į panašų klausimą.</p>
    <p><a href="/replies">← Grįžti į dashboard</a></p>
</div>
</body></html>"""
    return HTMLResponse(content=html)


@app.post("/api/rate/{interaction_id}")
async def rate_interaction(interaction_id: int, request: Request):
    if not _get_dashboard_session(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    body = await request.json()
    rating = body.get("rating")
    if rating not in ("thumbs_up", "thumbs_down"):
        return JSONResponse(status_code=400, content={"error": "rating must be thumbs_up or thumbs_down"})
    await update_rating(db, interaction_id, rating, body.get("override_text"), body.get("feedback_note"))
    return {"status": "ok", "interaction_id": interaction_id, "rating": rating}


@app.post("/api/human-takeover/{lead_email}/{campaign_id}")
async def human_takeover(lead_email: str, campaign_id: str, request: Request):
    if not _get_dashboard_session(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    await set_human_takeover(db, lead_email, campaign_id)
    return {"status": "ok", "lead_email": lead_email, "campaign_id": campaign_id}


@app.get("/api/stats")
async def stats():
    from datetime import timedelta
    since = (datetime.utcnow() - timedelta(days=30)).isoformat()
    data = await get_weekly_stats(db, since)
    return data


def main():
    parser = argparse.ArgumentParser(description="Gleadsy Reply Agent")
    parser.add_argument("--dev", action="store_true", help="Development mode with auto-reload")
    parser.add_argument("--test-classify", type=str, help="Test classification for a message")
    parser.add_argument("--test-reply", type=str, help="Test reply generation for a message")
    parser.add_argument("--client", type=str, default="gleadsy", help="Client ID for testing")
    args = parser.parse_args()

    if args.test_classify:
        from core.classifier import classify_reply
        clients_loaded = load_clients(config.CLIENTS_DIR)
        result = asyncio.run(classify_reply(args.test_classify, "test", 1))
        print(f"Category: {result.category}")
        print(f"Confidence: {result.confidence:.0%}")
        print(f"Reasoning: {result.reasoning}")
        return

    if args.test_reply:
        from core.classifier import classify_reply
        from core.reply_generator import generate_reply
        clients_loaded = load_clients(config.CLIENTS_DIR)
        client_config = clients_loaded.get(args.client)
        if not client_config:
            print(f"Client '{args.client}' not found. Available: {list(clients_loaded.keys())}")
            return
        cls = asyncio.run(classify_reply(args.test_reply, "test", 1))
        print(f"Classification: {cls.category} ({cls.confidence:.0%})")
        if cls.category in ("UNSUBSCRIBE", "OUT_OF_OFFICE", "UNCERTAIN"):
            print("No reply generated for this category.")
            return
        reply = asyncio.run(generate_reply(args.test_reply, cls.category, client_config, [], []))
        print(f"Reply:\n{reply}")
        return

    import uvicorn
    import os
    port = int(os.getenv("PORT", "8000"))
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=port,
        reload=args.dev,
    )


if __name__ == "__main__":
    main()
