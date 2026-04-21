"""Kasdieninis mokymosi digest'as Pauliui į Slack.

Rodo: kiek naujų few-shots, score'o trend'as, stiliaus profilio pokyčiai.
Paleidžiamas 2x/dieną: 08:00 (rytas) ir 20:00 (vakaras).
"""
import logging
from datetime import datetime, timedelta
from collections import Counter
import aiosqlite
import httpx
import config

logger = logging.getLogger(__name__)


async def _post_slack(text: str) -> None:
    if not config.SLACK_WEBHOOK_URL:
        logger.warning("SLACK_WEBHOOK_URL not set, digest not posted")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(config.SLACK_WEBHOOK_URL, json={"text": text})
    except Exception as e:
        logger.error(f"Slack digest post failed: {e}")


async def send_learning_digest(conn: aiosqlite.Connection) -> dict:
    """Siunčia mokymosi suvestinę į Slack. Grąžina stats."""
    conn.row_factory = aiosqlite.Row

    now = datetime.utcnow()
    day_ago = (now - timedelta(hours=24)).isoformat()
    week_ago = (now - timedelta(days=7)).isoformat()

    # Per paskutines 24h
    c = await conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE created_at >= ?", (day_ago,)
    )
    total_24h = (await c.fetchone())[0]

    c = await conn.execute(
        "SELECT COUNT(*) FROM interactions "
        "WHERE created_at >= ? AND human_rating='thumbs_up'",
        (day_ago,),
    )
    new_fs_24h = (await c.fetchone())[0]

    c = await conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE human_override_text IS NOT NULL AND created_at >= ?",
        (day_ago,),
    )
    overrides_24h = (await c.fetchone())[0]

    # Viso few-shots
    c = await conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE human_rating='thumbs_up' AND was_sent=1"
    )
    total_fs = (await c.fetchone())[0]

    # Score trend (šios savaitės vs paskutinės)
    c = await conn.execute(
        "SELECT AVG(quality_score) FROM interactions "
        "WHERE quality_score IS NOT NULL AND created_at >= ?",
        (week_ago,),
    )
    avg_score_week = (await c.fetchone())[0] or 0

    c = await conn.execute(
        "SELECT AVG(quality_score) FROM interactions "
        "WHERE quality_score IS NOT NULL AND created_at < ? AND created_at >= ?",
        (week_ago, (now - timedelta(days=14)).isoformat()),
    )
    avg_score_prev = (await c.fetchone())[0] or 0

    trend_arrow = "→"
    if avg_score_prev and avg_score_week:
        diff = avg_score_week - avg_score_prev
        trend_arrow = "⬆" if diff > 0.3 else ("⬇" if diff < -0.3 else "→")

    # Meetings booked + outcomes
    c = await conn.execute(
        "SELECT outcome, COUNT(*) FROM interactions "
        "WHERE created_at >= ? AND outcome IS NOT NULL GROUP BY outcome",
        (day_ago,),
    )
    outcomes = {row[0]: row[1] for row in await c.fetchall()}

    # Stiliaus signalai iš paskutinio savaitės fewshot'ų
    c = await conn.execute(
        "SELECT agent_reply FROM interactions "
        "WHERE human_rating='thumbs_up' AND was_sent=1 AND created_at >= ? "
        "LIMIT 50",
        (week_ago,),
    )
    replies = [dict(r)["agent_reply"] for r in await c.fetchall() if dict(r)["agent_reply"]]

    linkejimai = sum(1 for r in replies if "Linkėjimai" in r or "Linkejimai" in r)
    emoji_winky = sum(1 for r in replies if ";)" in r)
    avg_sent = 0
    if replies:
        import re
        sents = [len(re.split(r"[.!?]+", r)) for r in replies]
        avg_sent = sum(sents) / len(sents)

    # Progresas iki 1:1 mimikavimo (target: 50 pavyzdžių)
    target = 50
    progress_pct = min(100, int(total_fs / target * 100))
    progress_bar = "█" * (progress_pct // 5) + "░" * (20 - progress_pct // 5)

    text = f"""🎓 *Reply Agent - mokymosi suvestinė*

*Per paskutines 24h:*
• {total_24h} interakcijos
• {new_fs_24h} nauji pavyzdžiai (few-shots)
• {overrides_24h} perrašymai (tavo korekcijos)

*Progresas iki 1:1 braižo mimikavimo:*
`{progress_bar}` {progress_pct}% ({total_fs}/{target}+ pavyzdžių)

*Kokybės tendencija (7d):*
Vid. score: {avg_score_week:.1f}/10 {trend_arrow} (prieš savaitę: {avg_score_prev:.1f}/10)

*Outcomes (24h):*
• Meeting booked: {outcomes.get('meeting_booked', 0)}
• Replied again: {outcomes.get('replied_again', 0)}
• Went silent: {outcomes.get('went_silent', 0)}
• Unsubscribed: {outcomes.get('unsubscribed', 0)}

*Pauliaus stiliaus profilis (iš 7d pavyzdžių):*
• Sign-off „Linkėjimai": {linkejimai}/{len(replies)} atsakymų
• Emoji „;)": {emoji_winky}/{len(replies)} atsakymų
• Vid. sakinių: {avg_sent:.1f}

Dashboard: https://reply.gleadsy.com/learning
"""

    await _post_slack(text)
    return {
        "total_24h": total_24h,
        "new_fs_24h": new_fs_24h,
        "total_fs": total_fs,
        "avg_score_week": avg_score_week,
        "progress_pct": progress_pct,
    }
