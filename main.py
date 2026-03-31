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

    scheduler.add_job(lambda: asyncio.ensure_future(run_outcome_tracker(db)), "interval", hours=6, id="outcome_tracker")
    scheduler.add_job(lambda: asyncio.ensure_future(run_weekly_digest(db)), "cron", day_of_week="mon", hour=9, id="weekly_digest")
    scheduler.add_job(lambda: asyncio.ensure_future(run_confidence_calibrator(db)), "cron", day_of_week="sun", hour=23, id="confidence_calibrator")

    # Polling mode
    if config.REPLY_SOURCE == "polling":
        from core.instantly_client import poll_for_replies
        last_poll = {"timestamp": datetime.utcnow().isoformat()}

        async def poll_job():
            replies = await poll_for_replies(last_poll["timestamp"])
            last_poll["timestamp"] = datetime.utcnow().isoformat()
            for reply in replies:
                await handle_instantly_webhook(reply, db, clients, config.CONFIDENCE_THRESHOLD)

        scheduler.add_job(lambda: asyncio.ensure_future(poll_job()), "interval", seconds=config.POLLING_INTERVAL_SECONDS, id="poller")
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
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=8000,
        reload=args.dev,
    )


if __name__ == "__main__":
    main()
