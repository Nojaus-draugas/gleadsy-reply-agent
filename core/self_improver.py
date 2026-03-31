import aiosqlite


async def get_best_examples(conn: aiosqlite.Connection, category: str, client_id: str, limit: int = 3) -> list[dict]:
    """Get best few-shot examples, prioritized by quality."""
    cursor = await conn.execute(
        """SELECT id, prospect_message, agent_reply, human_rating, outcome
        FROM interactions
        WHERE client_id = ? AND classification = ? AND was_sent = 1
        AND (human_rating IS NULL OR human_rating != 'thumbs_down')
        ORDER BY
            CASE
                WHEN human_rating = 'thumbs_up' AND outcome = 'meeting_booked' THEN 1
                WHEN human_rating = 'thumbs_up' THEN 2
                WHEN outcome = 'meeting_booked' THEN 3
                WHEN outcome = 'replied_again' THEN 4
                ELSE 5
            END,
            created_at DESC
        LIMIT ?""",
        (client_id, category, limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def get_anti_patterns(conn: aiosqlite.Connection, category: str, client_id: str, limit: int = 2) -> list[dict]:
    """Get bad examples where human provided override."""
    cursor = await conn.execute(
        """SELECT prospect_message, agent_reply as bad_reply,
                human_override_text as correct_reply, human_feedback_note as feedback_note
        FROM interactions
        WHERE client_id = ? AND classification = ?
        AND human_rating = 'thumbs_down' AND human_override_text IS NOT NULL
        ORDER BY created_at DESC
        LIMIT ?""",
        (client_id, category, limit),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]
