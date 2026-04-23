import aiosqlite


async def get_best_examples(
    conn: aiosqlite.Connection,
    category: str,
    client_id: str,
    limit: int = 3,
    language: str | None = None,
) -> list[dict]:
    """Get best few-shot examples, prioritized by quality.

    If `language` is provided, matches that language OR legacy NULL-language
    examples. If None (default), no language filter (backward compat).
    """
    if language is None:
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
    else:
        cursor = await conn.execute(
            """SELECT id, prospect_message, agent_reply, human_rating, outcome
            FROM interactions
            WHERE client_id = ? AND classification = ? AND was_sent = 1
            AND (human_rating IS NULL OR human_rating != 'thumbs_down')
            AND (original_language = ? OR original_language IS NULL)
            ORDER BY
                CASE
                    WHEN original_language = ? THEN 0 ELSE 1
                END,
                CASE
                    WHEN human_rating = 'thumbs_up' AND outcome = 'meeting_booked' THEN 1
                    WHEN human_rating = 'thumbs_up' THEN 2
                    WHEN outcome = 'meeting_booked' THEN 3
                    WHEN outcome = 'replied_again' THEN 4
                    ELSE 5
                END,
                created_at DESC
            LIMIT ?""",
            (client_id, category, language, language, limit),
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
