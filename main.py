import argparse
import asyncio
import json
import logging
import sys
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

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

    scheduler.start()

    logger.info(f"Gleadsy Reply Agent paleistas (port 8000)")
    logger.info(f"Webhook endpoint: POST /webhook/instantly")
    logger.info(f"Laukiu reply'ų...")

    yield

    scheduler.shutdown()
    if db:
        await db.close()


app = FastAPI(title="Gleadsy Reply Agent", lifespan=lifespan)


@app.post("/webhook/instantly")
async def webhook_instantly(request: Request):
    payload = await request.json()
    result = await handle_instantly_webhook(payload, db, clients, config.CONFIDENCE_THRESHOLD)
    return JSONResponse(content=result)


@app.post("/webhook/slack")
async def webhook_slack(request: Request):
    # Placeholder for future Slack interactive components
    return JSONResponse(content={"status": "not_implemented"})


@app.get("/health")
async def health():
    return {"status": "ok", "clients": list(clients.keys()), "confidence_threshold": config.CONFIDENCE_THRESHOLD}


@app.get("/replies")
async def replies_dashboard():
    """Web dashboard showing all test mode replies."""
    import csv
    from fastapi.responses import HTMLResponse
    csv_path = Path(__file__).parent / "test_replies.csv"
    rows = []
    if csv_path.exists():
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)

    import html as html_mod
    rows_html = ""
    for r in reversed(rows):  # newest first
        orig = html_mod.escape(r.get('Original message',''))
        reply = html_mod.escape(r.get('Generated reply',''))
        rows_html += f"""<tr>
            <td>{html_mod.escape(r.get('Timestamp',''))}</td>
            <td>{html_mod.escape(r.get('Client ID',''))}</td>
            <td>{html_mod.escape(r.get('Lead email',''))}</td>
            <td>{html_mod.escape(r.get('Classification',''))}</td>
            <td>{html_mod.escape(r.get('Confidence',''))}</td>
            <td style="max-width:400px;white-space:pre-wrap">{orig}</td>
            <td style="max-width:400px;white-space:pre-wrap">{reply}</td>
            <td>{html_mod.escape(r.get('Status',''))}</td>
        </tr>"""

    html = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Gleadsy Reply Agent - Drafts</title>
<meta http-equiv="refresh" content="30">
<style>
body {{ font-family: -apple-system, sans-serif; margin: 20px; background: #f5f5f5; }}
h1 {{ color: #333; }}
table {{ border-collapse: collapse; width: 100%; background: white; box-shadow: 0 1px 3px rgba(0,0,0,0.1); }}
th {{ background: #4285f4; color: white; padding: 12px 8px; text-align: left; font-size: 13px; }}
td {{ padding: 10px 8px; border-bottom: 1px solid #eee; font-size: 13px; vertical-align: top; }}
tr:hover {{ background: #f0f7ff; }}
.badge {{ display: inline-block; padding: 2px 8px; border-radius: 12px; font-size: 11px; font-weight: bold; }}
.count {{ color: #666; font-size: 14px; margin: 10px 0; }}
</style></head><body>
<h1>Gleadsy Reply Agent - Test Drafts</h1>
<p class="count">{len(rows)} reply(-iai) | Auto-refresh kas 30s | TEST_MODE=true</p>
<table>
<tr><th>Laikas</th><th>Klientas</th><th>Lead</th><th>Klasifikacija</th><th>Confidence</th><th>Originali zinute</th><th>Sugeneruotas atsakymas</th><th>Statusas</th></tr>
{rows_html}
</table></body></html>"""
    return HTMLResponse(content=html)


@app.get("/answer/{interaction_id}")
async def answer_form(interaction_id: int):
    """Show form to answer an unknown question."""
    from fastapi.responses import HTMLResponse
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
    body = await request.json()
    rating = body.get("rating")
    if rating not in ("thumbs_up", "thumbs_down"):
        return JSONResponse(status_code=400, content={"error": "rating must be thumbs_up or thumbs_down"})
    await update_rating(db, interaction_id, rating, body.get("override_text"), body.get("feedback_note"))
    return {"status": "ok", "interaction_id": interaction_id, "rating": rating}


@app.post("/api/human-takeover/{lead_email}/{campaign_id}")
async def human_takeover(lead_email: str, campaign_id: str):
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
