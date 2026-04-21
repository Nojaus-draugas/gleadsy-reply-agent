import argparse
import asyncio
import json
import logging
import os
import secrets
import sys
from contextlib import asynccontextmanager
from datetime import datetime, timedelta
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
_webhook_state: dict = {}  # tracks last webhook receive time + alert flag


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

    # Auto-learn: kas 15 min traukia Pauliaus realius atsakymus iš Instantly ir
    # saugo kaip few-shot pavyzdzius. Sistema mokosi is kiekvieno naujo atsakymo
    # kol galės 1:1 atkartoti Pauliaus stilių.
    # Boost: kol DB < 50 pavyzdžių, paleidžiam kas 15 min (greitesnis mokymasis).
    async def run_auto_learn_job():
        from core.auto_learn import run_auto_learn
        try:
            await run_auto_learn(db, clients)
        except Exception as e:
            logger.error(f"auto_learn job failed: {e}")
    auto_learn_interval = int(os.getenv("AUTO_LEARN_INTERVAL_MINUTES", "15"))
    scheduler.add_job(run_auto_learn_job, "interval", minutes=auto_learn_interval, id="auto_learn")

    # Learning digest - Slack 2x/dieną (08:00 ir 20:00 Europe/Vilnius)
    async def run_learning_digest_job():
        from cron.learning_digest import send_learning_digest
        try:
            stats = await send_learning_digest(db)
            logger.info(f"learning_digest sent: {stats}")
        except Exception as e:
            logger.error(f"learning_digest failed: {e}")
    scheduler.add_job(run_learning_digest_job, "cron", hour=8, minute=0, id="learning_digest_morning")
    scheduler.add_job(run_learning_digest_job, "cron", hour=20, minute=0, id="learning_digest_evening")

    # Webhook-only mode. No polling - if Instantly webhook stops delivering,
    # webhook_silence_monitor below will page Slack.

    # Periodic health monitoring - check API keys, DB, notify Slack on issues
    async def health_monitor():
        from core.slack_notifier import notify_error
        issues = []

        # Check DB
        try:
            await db.execute("SELECT 1")
        except Exception as e:
            issues.append(f"DB error: {e}")

        # Check Anthropic API key validity (lightweight - just verify key format)
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

    # Webhook silence monitor - alerts if no Instantly webhook received during business hours.
    # Checks last received webhook timestamp (tracked on /webhook/instantly POST).
    # Fires once per silence streak; reset when a webhook arrives.
    async def webhook_silence_monitor():
        from core.slack_notifier import notify_error
        from datetime import datetime as _dt
        # Only alert during business hours Europe/Vilnius (~UTC+2/3)
        hour = (_dt.utcnow().hour + 3) % 24
        if hour < 9 or hour >= 18:
            return
        last_ts = _webhook_state.get("last_received_at")
        now = datetime.utcnow()
        # If service just started, give webhooks 2h to arrive before alerting
        reference = last_ts or _webhook_state.get("service_started_at") or now
        silence_h = (now - reference).total_seconds() / 3600
        threshold_h = float(os.getenv("WEBHOOK_SILENCE_ALERT_HOURS", "6"))
        if silence_h >= threshold_h and not _webhook_state.get("alerted_silence"):
            await notify_error(
                "webhook_silence",
                f"Instantly webhook nepasiekia serverio jau {silence_h:.1f}h. "
                f"Patikrink Instantly → Integrations → Webhooks.",
            )
            _webhook_state["alerted_silence"] = True
            logger.error(f"Webhook silence {silence_h:.1f}h - Slack alerted")

    _webhook_state["service_started_at"] = datetime.utcnow()
    scheduler.add_job(webhook_silence_monitor, "interval", minutes=30, id="webhook_silence_monitor")

    scheduler.start()

    # Startup warnings
    if not config.WEBHOOK_SECRET:
        logger.warning("⚠️  WEBHOOK_SECRET not set - webhook endpoint is UNPROTECTED!")
    if not config.DASHBOARD_PASSWORD:
        logger.warning("⚠️  DASHBOARD_PASSWORD not set - dashboard is UNPROTECTED!")
    if not config.ANTHROPIC_API_KEY:
        logger.error("❌ ANTHROPIC_API_KEY not set - system cannot function!")
    if not config.INSTANTLY_API_KEY:
        logger.error("❌ INSTANTLY_API_KEY not set - cannot send replies!")

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
        logger.warning("WEBHOOK_SECRET not set - webhook endpoint is unprotected!")
        return
    # Check header first, then query param
    token = request.headers.get("X-Webhook-Secret") or request.query_params.get("secret")
    if not token or not secrets.compare_digest(token, config.WEBHOOK_SECRET):
        raise HTTPException(status_code=401, detail="Invalid or missing webhook secret")


def _get_dashboard_session(request: Request) -> bool:
    """Check if user has valid dashboard session cookie."""
    if not config.DASHBOARD_PASSWORD:
        return True  # No password set - open access
    session = request.cookies.get("gleadsy_session")
    if not session or not secrets.compare_digest(session, _session_token):
        return False
    return True


# Session token - generated on startup, lives in memory
_session_token = secrets.token_urlsafe(32)


@app.post("/webhook/instantly")
async def webhook_instantly(request: Request):
    """Fire-and-forget: acknowledge webhook in <100ms, process in background.

    Instantly has a ~30s webhook timeout. Our full pipeline (classify + generate
    reply + quality review + send + log) can take 10-35s, causing Instantly to
    mark the webhook as failed and drop the event (no retries). By returning 200
    immediately and processing asynchronously, we guarantee Instantly never
    times out and no prospect replies are lost.

    Risk: if the background task crashes, Instantly won't know. Mitigated via
    exception handler that notifies Slack so we can investigate manually.
    """
    _verify_webhook_secret(request)
    payload = await request.json()
    _webhook_state["last_received_at"] = datetime.utcnow()
    _webhook_state["alerted_silence"] = False  # reset silence alert

    async def _bg_process():
        try:
            await handle_instantly_webhook(payload, db, clients, config.CONFIDENCE_THRESHOLD)
        except Exception as e:
            logger.exception("Background webhook processing failed")
            try:
                from core.slack_notifier import notify_error
                await notify_error(
                    "webhook_processing_failed",
                    f"{type(e).__name__}: {e} | lead={payload.get('lead_email','?')} email_id={payload.get('email_id','?')}",
                )
            except Exception:
                logger.exception("Failed to notify Slack about background failure")

    asyncio.create_task(_bg_process())
    return JSONResponse(content={"status": "accepted"})


@app.post("/webhook/slack")
async def webhook_slack(request: Request):
    # Placeholder for future Slack interactive components
    return JSONResponse(content={"status": "not_implemented"})


@app.get("/health")
async def health():
    """Enhanced health check - verifies DB connectivity and API key presence."""
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
    # Wrong password - show login with error
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


@app.post("/logout")
async def logout():
    """Clear session and redirect to login."""
    response = RedirectResponse(url="/login", status_code=302)
    response.delete_cookie("gleadsy_session")
    return response


@app.get("/learning")
async def learning_dashboard(request: Request):
    """Mokymosi progresas + Pauliaus stiliaus profilis."""
    from fastapi.responses import HTMLResponse
    import html as html_mod_lp
    from core.stylometry import analyze_paulius_style, learning_progress

    if not _get_dashboard_session(request):
        return RedirectResponse(url="/login", status_code=302)

    style = await analyze_paulius_style(db)
    progress = await learning_progress(db)

    # Style sections
    sign_offs_html = "".join(
        f'<li><strong>{html_mod_lp.escape(k)}</strong>: {v}x</li>'
        for k, v in (style.get("sign_offs") or {}).items()
    ) or "<li>(dar nėra duomenų)</li>"

    emojis_html = "".join(
        f'<li><code>{html_mod_lp.escape(k)}</code>: {v}x</li>'
        for k, v in (style.get("emojis") or {}).items()
    ) or "<li>(nenaudoja)</li>"

    first_phrases_html = "".join(
        f'<li><em>„{html_mod_lp.escape(k)}…"</em>: {v}x</li>'
        for k, v in (style.get("first_phrases") or {}).items()
    ) or "<li>(įvairios)</li>"

    weekly_rows = ""
    for w in progress.get("weekly_trend", []):
        avg = f"{w['avg_score']:.1f}" if w.get("avg_score") else "-"
        thumbs_pct = ""
        if w.get("total"):
            rated = (w.get("thumbs_up") or 0) + (w.get("thumbs_down") or 0)
            if rated:
                up_pct = 100 * (w.get("thumbs_up") or 0) / rated
                thumbs_pct = f"{up_pct:.0f}% 👍"
        weekly_rows += f"""<tr>
            <td>{w['week']}</td>
            <td>{w['total']}</td>
            <td><strong>{avg}</strong>/10</td>
            <td>{thumbs_pct}</td>
            <td>{w.get('meetings') or 0}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Mokymosi progresas</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 1000px; margin: 30px auto; padding: 0 20px; background: #f5f5f5; color: #333; }}
h1 {{ color: #1565c0; }}
h2 {{ margin-top: 32px; color: #333; border-bottom: 2px solid #e0e0e0; padding-bottom: 6px; }}
.back {{ color: #4285f4; text-decoration: none; font-size: 14px; }}
.stat-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 12px; margin-bottom: 20px; }}
.stat-card {{ background: white; padding: 18px; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
.stat-card .number {{ font-size: 28px; font-weight: 700; color: #1565c0; }}
.stat-card .label {{ font-size: 13px; color: #666; margin-top: 4px; }}
.card {{ background: white; padding: 18px 22px; border-radius: 10px; box-shadow: 0 1px 3px rgba(0,0,0,0.08); margin-bottom: 16px; }}
.card h3 {{ margin-top: 0; color: #333; font-size: 15px; }}
.card ul {{ margin: 8px 0; padding-left: 20px; color: #555; font-size: 13px; line-height: 1.8; }}
.grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 16px; }}
table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 1px 3px rgba(0,0,0,0.08); }}
th {{ background: #1565c0; color: white; padding: 10px 14px; text-align: left; font-size: 13px; }}
td {{ padding: 10px 14px; border-top: 1px solid #e0e0e0; font-size: 13px; }}
.desc {{ color: #666; font-size: 13px; margin-bottom: 8px; }}
code {{ background: #eee; padding: 1px 5px; border-radius: 3px; font-size: 12px; }}
</style></head><body>
<a href="/replies" class="back">&larr; Grįžti į dashboard</a>
<h1>🎓 Mokymosi progresas</h1>
<p class="desc">Kaip agent'as mokosi iš Pauliaus realių atsakymų ir artėja prie 1:1 stiliaus atkartojimo.</p>

<div class="stat-grid">
    <div class="stat-card">
        <div class="number">{progress.get("few_shot_bank_size", 0)}</div>
        <div class="label">Pauliaus pavyzdžių bankas (few-shots)</div>
    </div>
    <div class="stat-card">
        <div class="number">{progress.get("total_overrides_from_paulius", 0)}</div>
        <div class="label">Pauliaus perrašymų (mokymosi signalų)</div>
    </div>
    <div class="stat-card">
        <div class="number">{style.get("total_samples", 0)}</div>
        <div class="label">Stiliaus analyzės pavyzdžiai</div>
    </div>
    <div class="stat-card">
        <div class="number">{style.get("avg_sentences", 0)}</div>
        <div class="label">Vid. sakinių Pauliaus atsakyme</div>
    </div>
</div>

<h2>📊 Savaitinė kokybės eiga</h2>
<p class="desc">Jei score auga iš savaitės į savaitę - agent'as mokosi. Jei thumbs_up % kyla - artėja prie Pauliaus stiliaus.</p>
<table>
    <tr><th>Savaitė</th><th>Interakcijų</th><th>Vid. quality</th><th>Žmogaus įvertinimas</th><th>Susitikimų</th></tr>
    {weekly_rows or '<tr><td colspan="5">(dar nėra duomenų)</td></tr>'}
</table>

<h2>✍️ Pauliaus stiliaus profilis</h2>
<p class="desc">Ką agent'as išmoko apie Pauliaus rašymo stilių iš {style.get("total_samples",0)} pavyzdžių.</p>
<div class="grid-2">
    <div class="card">
        <h3>Sign-off'ai</h3>
        <ul>{sign_offs_html}</ul>
    </div>
    <div class="card">
        <h3>Emoji naudojimas ({style.get("uses_emojis_pct", 0)}% atsakymų)</h3>
        <ul>{emojis_html}</ul>
    </div>
    <div class="card">
        <h3>Pradžios frazės</h3>
        <ul>{first_phrases_html}</ul>
    </div>
    <div class="card">
        <h3>Ilgio metrikos</h3>
        <ul>
            <li>Vidutinis: <strong>{style.get("avg_sentences", 0)}</strong> sakinių</li>
            <li>Trumpiausias: {style.get("min_sentences", 0)} sakinių</li>
            <li>Ilgiausias: {style.get("max_sentences", 0)} sakinių</li>
        </ul>
    </div>
</div>

<h2>🔄 Kaip sistema mokosi</h2>
<div class="card">
    <ul>
        <li><strong>Auto-learn cron</strong> (kas 1h) - traukia naujus Pauliaus atsakymus iš Instantly → įrašo kaip <code>thumbs_up</code> few-shots.</li>
        <li><strong>Perrašymų detekcija</strong> - jei Paulius rankomis perrašo agent'o draftą, tai žymima kaip <code>thumbs_down</code> + <code>human_override_text</code> (anti-pattern).</li>
        <li><strong>Few-shot selection</strong> - prie kiekvienos naujos klasifikacijos parenkami top 3 Pauliaus pavyzdžiai pagal kategoriją → perduodami į LLM.</li>
        <li><strong>Quality reviewer'is</strong> - automatiškai siūlo „Ką patobulinti" kiekvienam atsakymui su score &lt; 8.</li>
        <li><strong>Confidence kalibracija</strong> (sekmadieniais 23:00) - pagal Pauliaus thumbs up/down kalibuoja klasifikavimo threshold.</li>
    </ul>
</div>

<br><a href="/replies" class="back">&larr; Grįžti į dashboard</a>
</body></html>"""
    return HTMLResponse(content=html)


@app.get("/replies")
async def replies_dashboard(request: Request):
    """Web dashboard showing all replies from DB with rating buttons, stats, and filters."""
    from fastapi.responses import HTMLResponse
    import html as html_mod
    from urllib.parse import urlencode

    # Auth check
    if not _get_dashboard_session(request):
        return RedirectResponse(url="/login", status_code=302)

    # --- Filters ---
    filter_client = request.query_params.get("client", "")
    filter_date_from = request.query_params.get("from", "")
    filter_date_to = request.query_params.get("to", "")
    page = max(1, int(request.query_params.get("page", "1")))
    per_page = 50

    where_clauses = []
    params = []
    if filter_client:
        where_clauses.append("client_id = ?")
        params.append(filter_client)
    if filter_date_from:
        where_clauses.append("created_at >= ?")
        params.append(filter_date_from)
    if filter_date_to:
        where_clauses.append("created_at <= ?")
        params.append(filter_date_to + " 23:59:59")

    where_sql = (" WHERE " + " AND ".join(where_clauses)) if where_clauses else ""

    # Total count for pagination
    cursor = await db.execute(f"SELECT COUNT(*) FROM interactions{where_sql}", params)
    total_count = (await cursor.fetchone())[0]
    total_pages = max(1, (total_count + per_page - 1) // per_page)
    offset = (page - 1) * per_page

    # Fetch rows
    cursor = await db.execute(
        f"SELECT id, created_at, client_id, lead_email, campaign_id, classification, confidence, "
        f"classification_reasoning, prospect_message, agent_reply, was_sent, human_rating, "
        f"quality_score, quality_summary, quality_issues, outcome "
        f"FROM interactions{where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?",
        params + [per_page, offset],
    )
    rows = [dict(r) for r in await cursor.fetchall()]

    # --- Stats (over filtered set) ---
    cursor = await db.execute(
        f"SELECT COUNT(*) as total, "
        f"SUM(CASE WHEN was_sent = 1 THEN 1 ELSE 0 END) as sent, "
        f"AVG(CASE WHEN quality_score IS NOT NULL THEN quality_score END) as avg_quality "
        f"FROM interactions{where_sql}", params,
    )
    stats_row = dict(await cursor.fetchone())
    stat_total = stats_row["total"] or 0
    stat_sent = stats_row["sent"] or 0
    stat_sent_pct = f"{stat_sent / stat_total * 100:.0f}%" if stat_total > 0 else "0%"
    stat_avg_quality = f"{stats_row['avg_quality']:.1f}" if stats_row["avg_quality"] else "-"

    # Awaiting prospect reply: distinct leads where we sent a reply and no subsequent prospect reply
    awaiting_where = where_sql + (" AND " if where_sql else " WHERE ") + "was_sent = 1 AND outcome IS NULL"
    cursor = await db.execute(
        f"SELECT COUNT(DISTINCT lead_email) FROM interactions{awaiting_where}", params,
    )
    stat_awaiting = (await cursor.fetchone())[0] or 0

    cursor = await db.execute(
        f"SELECT classification, COUNT(*) as cnt FROM interactions{where_sql} GROUP BY classification",
        params,
    )
    cls_counts = {row["classification"]: row["cnt"] for row in await cursor.fetchall()}

    # Classification mini-chart
    cls_colors_map = {
        "INTERESTED": "#2e7d32", "QUESTION": "#1565c0", "NOT_NOW": "#e65100",
        "REFERRAL": "#6a1b9a", "UNSUBSCRIBE": "#c62828", "OUT_OF_OFFICE": "#757575",
        "UNCERTAIN": "#f9a825", "API_ERROR": "#d32f2f",
    }
    cls_badges_html = " ".join(
        f'<span style="display:inline-block;padding:2px 8px;border-radius:10px;font-size:11px;'
        f'color:white;background:{cls_colors_map.get(c, "#333")};margin:2px">{c}: {n}</span>'
        for c, n in sorted(cls_counts.items(), key=lambda x: -x[1])
    )

    # Available clients for filter dropdown - merge DB clients with loaded YAML configs
    cursor = await db.execute("SELECT DISTINCT client_id FROM interactions ORDER BY client_id")
    db_clients = [row["client_id"] for row in await cursor.fetchall()]
    available_clients = sorted(set(db_clients) | set(clients.keys()))

    # --- Build table rows ---
    rows_html = ""
    for r in rows:
        iid = r["id"]
        orig = html_mod.escape(r.get("prospect_message") or "")
        reply = html_mod.escape(r.get("agent_reply") or "")
        classification = r.get("classification", "")
        confidence = r.get("confidence", 0)
        cls_reasoning_raw = r.get("classification_reasoning") or ""
        cls_reasoning = html_mod.escape(cls_reasoning_raw)
        quality_score = r.get("quality_score")
        quality_summary = html_mod.escape(r.get("quality_summary") or "")
        # Issues list -> plaukstanti info
        import json as _json_mod
        q_issues_raw = r.get("quality_issues") or "[]"
        try:
            q_issues_list = _json_mod.loads(q_issues_raw) if q_issues_raw else []
        except Exception:
            q_issues_list = []
        rating = r.get("human_rating") or ""
        lead_email = html_mod.escape(r.get("lead_email", ""))
        campaign_id = r.get("campaign_id", "")

        cls_color = cls_colors_map.get(classification, "#333")

        # Classification badge - su tooltip'u kodel taip klasifikuota
        cls_tooltip = f"Kodel: {cls_reasoning_raw} (conf: {confidence:.0%})" if cls_reasoning_raw else ""
        cls_badge_html = f'<span class="badge" style="color:white;background:{cls_color}" title="{html_mod.escape(cls_tooltip)}">{classification}</span>'

        # Quality badge - su tooltip'u kodel tiek
        if quality_score is not None:
            if quality_score >= 8:
                q_color, q_bg = "#2e7d32", "#e8f5e9"
            elif quality_score >= 6:
                q_color, q_bg = "#e65100", "#fff3e0"
            else:
                q_color, q_bg = "#c62828", "#ffebee"
            # Detalesnis tooltip: summary + issues
            q_tooltip_parts = []
            if quality_summary:
                q_tooltip_parts.append(f"Ivertis: {quality_summary}")
            if q_issues_list:
                q_tooltip_parts.append("Issues:\n- " + "\n- ".join(str(i) for i in q_issues_list))
            q_tooltip = "\n\n".join(q_tooltip_parts) if q_tooltip_parts else ""
            quality_badge = f'<span class="badge" style="color:{q_color};background:{q_bg};cursor:help" title="{html_mod.escape(q_tooltip)}">{quality_score}/10</span>'
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

        # Awaiting-reply status badge
        outcome = r.get("outcome")
        if r.get("was_sent") and not outcome:
            status_badge = '<span class="badge" style="color:#e65100;background:#fff3e0" title="Laukiam leado atsakymo">&#9203; Laukiam</span>'
        elif outcome == "replied_again":
            status_badge = '<span class="badge" style="color:#2e7d32;background:#e8f5e9" title="Leadas atsake">&#128172; Atsake</span>'
        elif outcome == "unsubscribed":
            status_badge = '<span class="badge" style="color:#c62828;background:#ffebee">Unsub</span>'
        else:
            status_badge = '<span class="badge" style="color:#999;background:#f5f5f5">-</span>'

        # Lead email links to conversation view
        lead_link = f'<a href="/conversation/{lead_email}/{campaign_id}" style="color:#1565c0;text-decoration:none" title="Rodyti visa pokalbio gija">{lead_email}</a>'

        rows_html += f"""<tr id="row-{iid}">
            <td>{html_mod.escape(str(r.get('created_at','')))}</td>
            <td>{html_mod.escape(r.get('client_id',''))}</td>
            <td>{lead_link}</td>
            <td>{cls_badge_html}</td>
            <td>{confidence:.0%}</td>
            <td>{quality_badge}</td>
            <td class="msg-col">{orig}</td>
            <td class="msg-col">{reply}</td>
            <td style="text-align:center">{sent_icon}</td>
            <td style="text-align:center">{status_badge}</td>
            <td style="text-align:center;white-space:nowrap">{rating_html}</td>
        </tr>"""

    # --- Pagination links ---
    def _page_url(p):
        qp = {}
        if filter_client:
            qp["client"] = filter_client
        if filter_date_from:
            qp["from"] = filter_date_from
        if filter_date_to:
            qp["to"] = filter_date_to
        qp["page"] = p
        return f"/replies?{urlencode(qp)}"

    pagination_html = '<div class="pagination">'
    if page > 1:
        pagination_html += f'<a href="{_page_url(page - 1)}">&laquo; Ankstesnis</a>'
    pagination_html += f'<span class="page-info">Puslapis {page} / {total_pages} ({total_count} viso)</span>'
    if page < total_pages:
        pagination_html += f'<a href="{_page_url(page + 1)}">Kitas &raquo;</a>'
    pagination_html += '</div>'

    # Client filter options
    client_options = '<option value="">Visi klientai</option>'
    for c in available_clients:
        selected = ' selected' if c == filter_client else ''
        client_options += f'<option value="{html_mod.escape(c)}"{selected}>{html_mod.escape(c)}</option>'

    test_mode_label = "TEST_MODE" if config.TEST_MODE else "LIVE"

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Gleadsy Reply Agent - Dashboard</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; margin: 0; padding: 20px; background: #f5f5f5; }}
.header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 8px; }}
h1 {{ color: #333; margin: 0; }}
.logout-btn {{ padding: 8px 16px; background: #e0e0e0; color: #333; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; }}
.logout-btn:hover {{ background: #bdbdbd; }}
table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); border-radius: 8px; overflow: hidden; }}
th {{ background: #4285f4; color: white; padding: 12px 8px; text-align: left; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; position: sticky; top: 0; }}
td {{ padding: 10px 8px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }}
tr:hover {{ background: #f0f7ff; }}
.badge {{ display: inline-block; padding: 3px 10px; border-radius: 12px; font-size: 11px; font-weight: 600; }}
.count {{ color: #666; font-size: 14px; margin: 4px 0 12px; }}
.msg-col {{ max-width: 350px; white-space: pre-wrap; word-break: break-word; }}
.rate-btn {{ border: none; background: none; font-size: 18px; cursor: pointer; padding: 4px 6px; border-radius: 6px; transition: background 0.2s; }}
.rate-btn:hover {{ background: #e3f2fd; }}
.rate-btn.up:hover {{ background: #e8f5e9; }}
.rate-btn.down:hover {{ background: #ffebee; }}
.rated {{ animation: flash 0.5s; }}
@keyframes flash {{ 0%,100% {{ background: inherit; }} 50% {{ background: #e8f5e9; }} }}
.stats {{ display: flex; gap: 16px; margin-bottom: 16px; flex-wrap: wrap; }}
.stat-card {{ background: white; padding: 14px 22px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); min-width: 100px; }}
.stat-card .number {{ font-size: 26px; font-weight: 700; color: #333; }}
.stat-card .label {{ font-size: 11px; color: #888; text-transform: uppercase; margin-top: 2px; }}
.filters {{ background: white; padding: 14px 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 16px; display: flex; gap: 12px; align-items: center; flex-wrap: wrap; }}
.filters label {{ font-size: 12px; color: #666; font-weight: 600; text-transform: uppercase; }}
.filters select, .filters input {{ padding: 6px 10px; border: 1px solid #ddd; border-radius: 6px; font-size: 13px; }}
.filters button {{ padding: 6px 16px; background: #4285f4; color: white; border: none; border-radius: 6px; cursor: pointer; font-size: 13px; }}
.filters button:hover {{ background: #3367d6; }}
.filters .reset {{ background: #e0e0e0; color: #333; }}
.filters .reset:hover {{ background: #bdbdbd; }}
.cls-breakdown {{ background: white; padding: 10px 20px; border-radius: 8px; box-shadow: 0 1px 3px rgba(0,0,0,0.1); margin-bottom: 16px; }}
.pagination {{ display: flex; justify-content: center; align-items: center; gap: 16px; margin-top: 16px; padding: 12px; }}
.pagination a {{ padding: 6px 14px; background: #4285f4; color: white; border-radius: 6px; text-decoration: none; font-size: 13px; }}
.pagination a:hover {{ background: #3367d6; }}
.page-info {{ font-size: 13px; color: #666; }}
</style></head><body>
<div class="header">
    <h1>Gleadsy Reply Agent</h1>
    <div style="display:flex;gap:8px;align-items:center">
        <a href="/learning" style="padding:8px 14px;background:#1565c0;color:white;border-radius:6px;text-decoration:none;font-size:13px;font-weight:600">🎓 Mokymosi progresas</a>
        <form method="POST" action="/logout" style="margin:0">
            <button type="submit" class="logout-btn">Atsijungti</button>
        </form>
    </div>
</div>
<p class="count">Auto-refresh kas 30s | {test_mode_label}</p>

<div class="stats">
    <div class="stat-card"><div class="number">{stat_total}</div><div class="label">Viso reply'u</div></div>
    <div class="stat-card"><div class="number">{stat_sent}</div><div class="label">Issiusta</div></div>
    <div class="stat-card"><div class="number">{stat_sent_pct}</div><div class="label">Siuntimo %</div></div>
    <div class="stat-card"><div class="number">{stat_avg_quality}</div><div class="label">Vid. quality</div></div>
    <div class="stat-card" style="border-left:4px solid #f9a825"><div class="number" style="color:#e65100">{stat_awaiting}</div><div class="label">Laukiam atsakymo</div></div>
</div>

<div class="cls-breakdown">{cls_badges_html}</div>

<div class="filters">
    <form method="GET" action="/replies" style="display:flex;gap:12px;align-items:center;flex-wrap:wrap;margin:0">
        <label>Klientas:</label>
        <select name="client">{client_options}</select>
        <label>Nuo:</label>
        <input type="date" name="from" value="{filter_date_from}">
        <label>Iki:</label>
        <input type="date" name="to" value="{filter_date_to}">
        <button type="submit">Filtruoti</button>
        <a href="/replies" class="reset" style="padding:6px 16px;background:#e0e0e0;color:#333;border-radius:6px;text-decoration:none;font-size:13px">Isvalyti</a>
    </form>
</div>

<table>
<tr><th>Laikas</th><th>Klientas</th><th>Lead</th><th>Kategorija</th><th>Conf.</th><th>Quality</th><th>Lead zinute</th><th>Atsakymas</th><th>Sent</th><th>Statusas</th><th>Vertinimas</th></tr>
{rows_html}
</table>

{pagination_html}

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


@app.get("/conversation/{lead_email}/{campaign_id}")
async def conversation_view(lead_email: str, campaign_id: str, request: Request):
    """Show full conversation thread for a lead + campaign."""
    from fastapi.responses import HTMLResponse
    import html as html_mod

    if not _get_dashboard_session(request):
        return RedirectResponse(url="/login", status_code=302)

    cursor = await db.execute(
        "SELECT id, created_at, classification, confidence, classification_reasoning, "
        "prospect_message, agent_reply, was_sent, human_rating, "
        "quality_score, quality_summary, quality_issues, improvement_suggestion, "
        "few_shots_used, thread_position "
        "FROM interactions WHERE lead_email = ? AND campaign_id = ? ORDER BY created_at ASC",
        (lead_email, campaign_id),
    )
    rows = [dict(r) for r in await cursor.fetchall()]

    if not rows:
        return HTMLResponse("<h2>Pokalbis nerastas</h2>", status_code=404)

    cls_colors_map = {
        "INTERESTED": "#2e7d32", "QUESTION": "#1565c0", "NOT_NOW": "#e65100",
        "REFERRAL": "#6a1b9a", "UNSUBSCRIBE": "#c62828", "OUT_OF_OFFICE": "#757575",
        "UNCERTAIN": "#f9a825", "API_ERROR": "#d32f2f",
    }

    import json as json_mod

    messages_html = ""
    for r in rows:
        cls = r.get("classification", "")
        cls_color = cls_colors_map.get(cls, "#333")
        prospect_msg = html_mod.escape(r.get("prospect_message") or "")
        agent_reply = html_mod.escape(r.get("agent_reply") or "")
        quality_score = r.get("quality_score")
        q_text = f" | Quality: {quality_score}/10" if quality_score is not None else ""
        sent_text = "Issiusta" if r.get("was_sent") else "Neissiusta"
        rating = r.get("human_rating") or ""
        rating_icon = ""
        if rating == "thumbs_up":
            rating_icon = " &#128077;"
        elif rating == "thumbs_down":
            rating_icon = " &#128078;"

        # Kodel taip ivertinta - sekcija paaiskinimui
        cls_reasoning = html_mod.escape(r.get("classification_reasoning") or "")
        q_summary = html_mod.escape(r.get("quality_summary") or "")
        improvement = html_mod.escape(r.get("improvement_suggestion") or "")
        q_issues_raw = r.get("quality_issues") or "[]"
        try:
            q_issues_list = json_mod.loads(q_issues_raw) if q_issues_raw else []
        except Exception:
            q_issues_list = []
        q_issues_html = "".join(f"<li>{html_mod.escape(str(i))}</li>" for i in q_issues_list)

        fs_raw = r.get("few_shots_used") or "[]"
        try:
            fs_list = json_mod.loads(fs_raw) if fs_raw else []
        except Exception:
            fs_list = []
        fs_count = len(fs_list)

        # Quality badge color
        if quality_score is not None:
            if quality_score >= 8:
                q_badge_color, q_badge_bg = "#2e7d32", "#e8f5e9"
            elif quality_score >= 6:
                q_badge_color, q_badge_bg = "#f57c00", "#fff8e1"
            else:
                q_badge_color, q_badge_bg = "#c62828", "#ffebee"
        else:
            q_badge_color, q_badge_bg = "#999", "#f5f5f5"

        messages_html += f"""
        <div class="msg lead-msg">
            <div class="msg-header">
                <strong>Lead</strong> - {html_mod.escape(str(r.get('created_at', '')))}
                <span class="badge" style="color:white;background:{cls_color};margin-left:8px">{cls}</span>
                <span style="color:#888;font-size:11px;margin-left:8px">{r.get('confidence',0):.0%}{q_text}</span>
            </div>
            <div class="msg-body">{prospect_msg}</div>
        </div>"""
        if agent_reply:
            # Detali "Kodel taip ivertinta" sekcija
            why_parts = []
            if cls_reasoning:
                why_parts.append(
                    f'<div class="why-row"><span class="why-label">Kodel klasifikuota kaip {cls}:</span>'
                    f'<span class="why-value">{cls_reasoning} <em>(conf: {r.get("confidence",0):.0%})</em></span></div>'
                )
            if quality_score is not None:
                q_label = "Puiku" if quality_score >= 8 else ("Priimtina" if quality_score >= 6 else "Zema kokybe")
                why_parts.append(
                    f'<div class="why-row"><span class="why-label">Quality ivertis <span class="q-badge" style="color:{q_badge_color};background:{q_badge_bg}">{quality_score}/10 - {q_label}</span>:</span>'
                    f'<span class="why-value">{q_summary or "(nera paaiskinimo)"}</span></div>'
                )
            if q_issues_list:
                why_parts.append(
                    f'<div class="why-row"><span class="why-label">Issues:</span>'
                    f'<ul class="why-issues">{q_issues_html}</ul></div>'
                )
            if fs_count:
                why_parts.append(
                    f'<div class="why-row"><span class="why-label">Pavyzdziai (few-shots):</span>'
                    f'<span class="why-value">{fs_count} istorini{"ai" if fs_count==1 else "u"} Pauliaus atsakymu panaudoti kaip kontekstas (IDs: {", ".join(str(x) for x in fs_list[:5])}{"..." if fs_count>5 else ""})</span></div>'
                )
            why_html = ""
            if why_parts:
                why_html = f'''
            <details class="why-details">
                <summary>Kodel taip ivertinta?</summary>
                <div class="why-body">{"".join(why_parts)}</div>
            </details>'''

            # Prominent "Ka patobulinti" kortele - rodoma TIK jei yra pasiulymas (paprastai score < 8)
            improve_html = ""
            if improvement:
                improve_html = f'''
            <div class="improve-card">
                <div class="improve-header">
                    <span class="improve-icon">&#128161;</span>
                    <strong>Ka patobulinti</strong>
                </div>
                <div class="improve-body">{improvement}</div>
            </div>'''

            messages_html += f"""
        <div class="msg agent-msg">
            <div class="msg-header">
                <strong>Agent</strong> - {sent_text}{rating_icon}
                <span class="q-badge-inline" style="color:{q_badge_color};background:{q_badge_bg};margin-left:8px">{quality_score if quality_score is not None else "-"}/10</span>
            </div>
            <div class="msg-body">{agent_reply}</div>{improve_html}{why_html}
        </div>"""

    safe_email = html_mod.escape(lead_email)
    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Pokalbis - {safe_email}</title>
<style>
body {{ font-family: -apple-system, sans-serif; max-width: 800px; margin: 30px auto; padding: 0 20px; background: #f5f5f5; }}
h2 {{ color: #333; }}
.back {{ color: #4285f4; text-decoration: none; font-size: 14px; }}
.back:hover {{ text-decoration: underline; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
.msg {{ margin: 12px 0; padding: 14px 18px; border-radius: 10px; }}
.lead-msg {{ background: white; border-left: 4px solid #1565c0; box-shadow: 0 1px 2px rgba(0,0,0,0.08); }}
.agent-msg {{ background: #e8f5e9; border-left: 4px solid #2e7d32; box-shadow: 0 1px 2px rgba(0,0,0,0.08); margin-left: 40px; }}
.msg-header {{ font-size: 12px; color: #666; margin-bottom: 6px; }}
.msg-body {{ white-space: pre-wrap; word-break: break-word; font-size: 14px; line-height: 1.5; }}
.summary {{ background: white; padding: 14px 20px; border-radius: 8px; margin-bottom: 16px; box-shadow: 0 1px 2px rgba(0,0,0,0.08); font-size: 13px; color: #555; }}
.q-badge-inline {{ display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 11px; font-weight: 600; }}
.q-badge {{ display: inline-block; padding: 1px 6px; border-radius: 8px; font-size: 10px; font-weight: 600; margin-left: 4px; }}
.why-details {{ margin-top: 10px; padding: 10px 14px; background: rgba(0,0,0,0.03); border-radius: 6px; font-size: 12px; }}
.why-details summary {{ cursor: pointer; color: #1565c0; font-weight: 600; user-select: none; }}
.why-details summary:hover {{ color: #0d47a1; }}
.why-details[open] summary {{ margin-bottom: 8px; }}
.why-body {{ padding-top: 4px; }}
.why-row {{ margin: 8px 0; padding: 6px 0; border-top: 1px solid rgba(0,0,0,0.05); }}
.why-row:first-child {{ border-top: none; padding-top: 0; }}
.why-label {{ display: block; font-weight: 600; color: #333; font-size: 11px; margin-bottom: 3px; text-transform: uppercase; letter-spacing: 0.5px; }}
.why-value {{ color: #555; line-height: 1.5; }}
.why-value em {{ color: #888; font-style: normal; font-size: 10px; }}
.why-issues {{ margin: 4px 0 0 0; padding-left: 18px; color: #c62828; }}
.why-issues li {{ margin: 2px 0; }}
.improve-card {{ margin-top: 10px; padding: 12px 14px; background: #fff8e1; border-left: 3px solid #f9a825; border-radius: 4px; font-size: 13px; }}
.improve-header {{ display: flex; align-items: center; gap: 6px; color: #e65100; font-weight: 600; margin-bottom: 6px; font-size: 12px; text-transform: uppercase; letter-spacing: 0.5px; }}
.improve-icon {{ font-size: 16px; }}
.improve-body {{ color: #5d4037; line-height: 1.5; white-space: pre-wrap; }}
</style></head><body>
<a href="/replies" class="back">&larr; Grizti i dashboard</a>
<h2>Pokalbis su {safe_email}</h2>
<div class="summary">
    <strong>Kampanija:</strong> {html_mod.escape(campaign_id[:12])}... |
    <strong>Zinuciu:</strong> {len(rows)} |
    <strong>Paskutine:</strong> {html_mod.escape(str(rows[-1].get('created_at', '')))}
</div>
{messages_html}
<br><a href="/replies" class="back">&larr; Grizti i dashboard</a>
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


@app.post("/admin/backfill-sheets")
async def admin_backfill_sheets(request: Request):
    """One-shot: push all existing interactions in DB into the Sheets backup."""
    if not _get_dashboard_session(request):
        raise HTTPException(status_code=401, detail="Not authenticated")
    from core import sheets_backup
    cursor = await db.execute("SELECT * FROM interactions ORDER BY id")
    rows = await cursor.fetchall()
    existing_ids = {r.get("id") for r in sheets_backup.fetch_all_rows() if r.get("id")}
    pushed = 0
    skipped = 0
    for r in rows:
        d = dict(r)
        if str(d.get("id")) in existing_ids:
            skipped += 1
            continue
        sheets_backup.append_interaction(d)
        pushed += 1
    return {"pushed": pushed, "skipped": skipped, "total_in_db": len(rows)}


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
