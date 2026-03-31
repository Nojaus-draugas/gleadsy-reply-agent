import logging
from db.database import get_stale_interactions, get_replied_again_missing, update_outcome

logger = logging.getLogger(__name__)


async def run_outcome_tracker(db):
    """Update outcomes: went_silent (7d no reply), replied_again catch-up."""
    # went_silent
    stale = await get_stale_interactions(db, days=7)
    for interaction in stale:
        await update_outcome(db, interaction["id"], "went_silent")
    if stale:
        logger.info(f"Outcome tracker: {len(stale)} interactions marked went_silent")

    # replied_again catch-up
    missed = await get_replied_again_missing(db)
    for interaction in missed:
        await update_outcome(db, interaction["id"], "replied_again")
    if missed:
        logger.info(f"Outcome tracker: {len(missed)} interactions marked replied_again")
