import logging
from datetime import datetime, timedelta
from db.database import get_weekly_stats
from core.slack_notifier import send_weekly_digest
import config

logger = logging.getLogger(__name__)


async def run_weekly_digest(db):
    """Send weekly stats to Slack."""
    now = datetime.utcnow()
    week_start = (now - timedelta(days=7)).strftime("%Y-%m-%d")
    week_end = now.strftime("%Y-%m-%d")
    since = (now - timedelta(days=7)).isoformat()

    stats = await get_weekly_stats(db, since)

    # Get last confidence change from DB
    cursor = await db.execute(
        "SELECT old_threshold, new_threshold FROM confidence_log ORDER BY created_at DESC LIMIT 1"
    )
    row = await cursor.fetchone()
    if row:
        conf_old, conf_new = row["old_threshold"], row["new_threshold"]
    else:
        conf_old = conf_new = config.CONFIDENCE_THRESHOLD

    await send_weekly_digest(stats, week_start, week_end, conf_old, conf_new)
    logger.info(f"Weekly digest sent: {stats.get('total', 0)} total replies")
