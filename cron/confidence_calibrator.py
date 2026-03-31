import logging
from datetime import datetime, timedelta
from db.database import get_rated_interactions_since, log_confidence_change
import config

logger = logging.getLogger(__name__)

MIN_SAMPLE_SIZE = 5
MIN_THRESHOLD = 0.5
MAX_THRESHOLD = 0.9


async def run_confidence_calibrator(db) -> float:
    """Recalculate confidence threshold based on recent ratings. Returns new threshold."""
    since = (datetime.utcnow() - timedelta(days=7)).isoformat()
    rated = await get_rated_interactions_since(db, since)

    if len(rated) < MIN_SAMPLE_SIZE:
        logger.info(f"Confidence calibrator: only {len(rated)} rated interactions, skipping (min {MIN_SAMPLE_SIZE})")
        return config.CONFIDENCE_THRESHOLD

    old_threshold = config.CONFIDENCE_THRESHOLD
    new_threshold = old_threshold

    thumbs_down_high_conf = [r for r in rated if r["human_rating"] == "thumbs_down" and r["confidence"] > 0.85]
    thumbs_up_low_conf = [r for r in rated if r["human_rating"] == "thumbs_up" and 0.6 <= r["confidence"] <= 0.7]

    if thumbs_down_high_conf:
        new_threshold += 0.05

    if thumbs_up_low_conf:
        new_threshold -= 0.03

    new_threshold = max(MIN_THRESHOLD, min(MAX_THRESHOLD, round(new_threshold, 2)))

    if new_threshold != old_threshold:
        config.CONFIDENCE_THRESHOLD = new_threshold
        thumbs_up_count = sum(1 for r in rated if r["human_rating"] == "thumbs_up")
        thumbs_down_count = sum(1 for r in rated if r["human_rating"] == "thumbs_down")
        uncertain_count = sum(1 for r in rated if r["classification"] == "UNCERTAIN")

        await log_confidence_change(db, {
            "week_start": (datetime.utcnow() - timedelta(days=7)).strftime("%Y-%m-%d"),
            "old_threshold": old_threshold,
            "new_threshold": new_threshold,
            "thumbs_up_count": thumbs_up_count,
            "thumbs_down_count": thumbs_down_count,
            "uncertain_count": uncertain_count,
            "reasoning": f"{len(thumbs_down_high_conf)} thumbs_down >0.85, {len(thumbs_up_low_conf)} thumbs_up 0.6-0.7",
        })
        logger.info(f"Confidence threshold: {old_threshold} → {new_threshold}")

    return new_threshold
