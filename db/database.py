import aiosqlite
from pathlib import Path
from datetime import datetime, timedelta

SCHEMA = """
CREATE TABLE IF NOT EXISTS interactions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    campaign_id TEXT NOT NULL,
    campaign_name TEXT,
    lead_email TEXT NOT NULL,
    email_account TEXT,
    email_id TEXT NOT NULL UNIQUE,
    client_id TEXT NOT NULL,
    prospect_message TEXT NOT NULL,
    classification TEXT NOT NULL,
    confidence REAL NOT NULL,
    classification_reasoning TEXT,
    agent_reply TEXT,
    was_sent BOOLEAN NOT NULL DEFAULT 0,
    matched_faq_index INTEGER,
    faq_confidence REAL,
    offered_slots TEXT,
    human_rating TEXT,
    human_override_text TEXT,
    human_feedback_note TEXT,
    outcome TEXT,
    outcome_updated_at TIMESTAMP,
    few_shots_used TEXT,
    thread_position INTEGER NOT NULL DEFAULT 1,
    brief_version TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS meetings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    interaction_id INTEGER NOT NULL REFERENCES interactions(id),
    lead_email TEXT NOT NULL,
    client_id TEXT NOT NULL,
    calendar_event_id TEXT,
    meeting_time TIMESTAMP NOT NULL,
    duration_minutes INTEGER NOT NULL,
    google_meet_link TEXT,
    status TEXT DEFAULT 'scheduled',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS confidence_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    week_start DATE NOT NULL,
    old_threshold REAL NOT NULL,
    new_threshold REAL NOT NULL,
    thumbs_up_count INTEGER,
    thumbs_down_count INTEGER,
    uncertain_count INTEGER,
    reasoning TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS human_takeovers (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lead_email TEXT NOT NULL,
    campaign_id TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(lead_email, campaign_id)
);

CREATE INDEX IF NOT EXISTS idx_interactions_lead ON interactions(lead_email, campaign_id);
CREATE INDEX IF NOT EXISTS idx_interactions_client ON interactions(client_id);
CREATE INDEX IF NOT EXISTS idx_interactions_classification ON interactions(classification);
CREATE INDEX IF NOT EXISTS idx_interactions_rating ON interactions(human_rating);
CREATE INDEX IF NOT EXISTS idx_interactions_outcome ON interactions(outcome);
CREATE INDEX IF NOT EXISTS idx_interactions_created ON interactions(created_at);
CREATE INDEX IF NOT EXISTS idx_interactions_email_id ON interactions(email_id);
"""


MIGRATIONS = [
    "ALTER TABLE interactions ADD COLUMN quality_score INTEGER",
    "ALTER TABLE interactions ADD COLUMN quality_issues TEXT",
    "ALTER TABLE interactions ADD COLUMN quality_summary TEXT",
    # Cost tracking - pridedama 2026-04
    "ALTER TABLE interactions ADD COLUMN tokens_in INTEGER",
    "ALTER TABLE interactions ADD COLUMN tokens_out INTEGER",
    "ALTER TABLE interactions ADD COLUMN tokens_cache_read INTEGER",
    "ALTER TABLE interactions ADD COLUMN cost_usd REAL",
    # 2026-04-21 - pasiulymas ka patobulinti (is quality reviewer'io)
    "ALTER TABLE interactions ADD COLUMN improvement_suggestion TEXT",
    # 2026-04-23 - foreign-language approval + translation
    "ALTER TABLE interactions ADD COLUMN original_language TEXT",
    "ALTER TABLE interactions ADD COLUMN prospect_message_lt TEXT",
    "ALTER TABLE interactions ADD COLUMN agent_reply_lt TEXT",
    "ALTER TABLE interactions ADD COLUMN approval_status TEXT",
    "ALTER TABLE interactions ADD COLUMN approved_at TIMESTAMP",
    "ALTER TABLE interactions ADD COLUMN approved_by TEXT",
    "ALTER TABLE interactions ADD COLUMN edit_history TEXT",
    "ALTER TABLE interactions ADD COLUMN final_sent_text TEXT",
    "CREATE INDEX IF NOT EXISTS idx_interactions_approval ON interactions(approval_status)",
]


async def _run_migrations(conn: aiosqlite.Connection):
    for sql in MIGRATIONS:
        try:
            await conn.execute(sql)
        except Exception:
            pass  # Column already exists
    await conn.commit()


async def init_db(db_path: Path) -> aiosqlite.Connection:
    is_fresh = not db_path.exists()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = await aiosqlite.connect(str(db_path))
    conn.row_factory = aiosqlite.Row
    await conn.executescript(SCHEMA)
    await _run_migrations(conn)
    await conn.commit()
    if is_fresh:
        await _restore_from_backup(conn)
    return conn


async def _restore_from_backup(conn: aiosqlite.Connection) -> None:
    """On fresh DB (e.g. after Render redeploy), pull prior interactions from Sheets."""
    import logging
    log = logging.getLogger(__name__)
    try:
        from core import sheets_backup
        rows = sheets_backup.fetch_all_rows()
        if not rows:
            return
        log.info("Restoring %d interactions from Google Sheets backup", len(rows))
        restored = 0
        for r in rows:
            try:
                def _v(k):
                    v = r.get(k, "")
                    return v if v != "" else None
                def _f(k):
                    v = _v(k)
                    try: return float(v) if v is not None else None
                    except: return None
                def _i(k):
                    v = _v(k)
                    try: return int(v) if v is not None else None
                    except: return None
                def _b(k):
                    v = str(r.get(k, "")).lower()
                    return v in ("1", "true", "yes")
                await conn.execute(
                    """INSERT OR IGNORE INTO interactions
                    (id, campaign_id, campaign_name, lead_email, email_account, email_id,
                     client_id, prospect_message, classification, confidence,
                     classification_reasoning, agent_reply, was_sent, matched_faq_index,
                     faq_confidence, offered_slots, few_shots_used, thread_position, brief_version,
                     quality_score, quality_issues, quality_summary, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (
                        _i("id"), _v("campaign_id"), _v("campaign_name"), _v("lead_email"),
                        _v("email_account"), _v("email_id"), _v("client_id"),
                        _v("prospect_message"), _v("classification"), _f("confidence"),
                        _v("classification_reasoning"), _v("agent_reply"),
                        _b("was_sent"), _i("matched_faq_index"), _f("faq_confidence"),
                        _v("offered_slots"), _v("few_shots_used"),
                        _i("thread_position") or 1, _v("brief_version"),
                        _f("quality_score"), _v("quality_issues"), _v("quality_summary"),
                        _v("created_at"),
                    ),
                )
                restored += 1
            except Exception as e:
                log.warning("restore row failed: %s", e)
        await conn.commit()
        log.info("Restored %d/%d interactions from backup", restored, len(rows))
    except Exception as e:
        log.error("restore_from_backup failed: %s", e)


async def log_interaction(conn: aiosqlite.Connection, data: dict) -> int:
    # Auto-pull per-request Claude usage jei call site nenurodo
    if "cost_usd" not in data:
        try:
            from core.classifier import get_usage_snapshot
            snap = get_usage_snapshot()
            data = {**data, **snap}
        except Exception:
            pass
    cursor = await conn.execute(
        """INSERT INTO interactions
        (campaign_id, campaign_name, lead_email, email_account, email_id,
         client_id, prospect_message, classification, confidence,
         classification_reasoning, agent_reply, was_sent, matched_faq_index,
         faq_confidence, offered_slots, few_shots_used, thread_position, brief_version,
         quality_score, quality_issues, quality_summary, improvement_suggestion,
         tokens_in, tokens_out, tokens_cache_read, cost_usd)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["campaign_id"], data.get("campaign_name"), data["lead_email"],
            data.get("email_account"), data["email_id"], data["client_id"],
            data["prospect_message"], data["classification"], data["confidence"],
            data.get("classification_reasoning"), data.get("agent_reply"),
            data.get("was_sent", False), data.get("matched_faq_index"),
            data.get("faq_confidence"), data.get("offered_slots"),
            data.get("few_shots_used"), data.get("thread_position", 1),
            data.get("brief_version"),
            data.get("quality_score"), data.get("quality_issues"),
            data.get("quality_summary"), data.get("improvement_suggestion"),
            data.get("tokens_in"), data.get("tokens_out"),
            data.get("tokens_cache_read"), data.get("cost_usd"),
        ),
    )
    await conn.commit()
    row_id = cursor.lastrowid
    # Backup to Google Sheets (silent if not configured)
    try:
        from core import sheets_backup
        from datetime import datetime, timezone
        backup_row = dict(data)
        backup_row["id"] = row_id
        backup_row["created_at"] = datetime.now(timezone.utc).isoformat()
        sheets_backup.append_interaction(backup_row)
    except Exception:
        pass
    return row_id


async def is_duplicate(conn: aiosqlite.Connection, email_id: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM interactions WHERE email_id = ?", (email_id,)
    )
    return await cursor.fetchone() is not None


async def get_thread_reply_count(conn: aiosqlite.Connection, lead_email: str, campaign_id: str) -> int:
    cursor = await conn.execute(
        "SELECT COUNT(*) FROM interactions WHERE lead_email = ? AND campaign_id = ? AND was_sent = 1",
        (lead_email, campaign_id),
    )
    row = await cursor.fetchone()
    return row[0]


async def reply_sent_within_cooldown(conn: aiosqlite.Connection, lead_email: str, campaign_id: str, hours: int) -> bool:
    cutoff = (datetime.utcnow() - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    cursor = await conn.execute(
        "SELECT 1 FROM interactions WHERE lead_email = ? AND campaign_id = ? AND was_sent = 1 AND created_at > ?",
        (lead_email, campaign_id, cutoff),
    )
    return await cursor.fetchone() is not None


async def get_interactions_for_lead(conn: aiosqlite.Connection, lead_email: str, campaign_id: str) -> list[dict]:
    cursor = await conn.execute(
        "SELECT * FROM interactions WHERE lead_email = ? AND campaign_id = ? ORDER BY created_at ASC",
        (lead_email, campaign_id),
    )
    rows = await cursor.fetchall()
    return [dict(r) for r in rows]


async def update_rating(conn: aiosqlite.Connection, interaction_id: int, rating: str, override_text: str | None, feedback_note: str | None):
    await conn.execute(
        "UPDATE interactions SET human_rating = ?, human_override_text = ?, human_feedback_note = ? WHERE id = ?",
        (rating, override_text, feedback_note, interaction_id),
    )
    await conn.commit()


async def update_outcome(conn: aiosqlite.Connection, interaction_id: int, outcome: str):
    await conn.execute(
        "UPDATE interactions SET outcome = ?, outcome_updated_at = ? WHERE id = ?",
        (outcome, datetime.utcnow().isoformat(), interaction_id),
    )
    await conn.commit()


async def is_human_takeover(conn: aiosqlite.Connection, lead_email: str, campaign_id: str) -> bool:
    cursor = await conn.execute(
        "SELECT 1 FROM human_takeovers WHERE lead_email = ? AND campaign_id = ?",
        (lead_email, campaign_id),
    )
    return await cursor.fetchone() is not None


async def set_human_takeover(conn: aiosqlite.Connection, lead_email: str, campaign_id: str):
    await conn.execute(
        "INSERT OR IGNORE INTO human_takeovers (lead_email, campaign_id) VALUES (?, ?)",
        (lead_email, campaign_id),
    )
    await conn.commit()


async def get_last_offered_slots(conn: aiosqlite.Connection, lead_email: str, campaign_id: str) -> str | None:
    cursor = await conn.execute(
        "SELECT offered_slots FROM interactions WHERE lead_email = ? AND campaign_id = ? AND offered_slots IS NOT NULL ORDER BY created_at DESC LIMIT 1",
        (lead_email, campaign_id),
    )
    row = await cursor.fetchone()
    return row["offered_slots"] if row else None


async def log_meeting(conn: aiosqlite.Connection, data: dict) -> int:
    cursor = await conn.execute(
        """INSERT INTO meetings
        (interaction_id, lead_email, client_id, calendar_event_id,
         meeting_time, duration_minutes, google_meet_link, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            data["interaction_id"], data["lead_email"], data["client_id"],
            data.get("calendar_event_id"), data["meeting_time"],
            data["duration_minutes"], data.get("google_meet_link"), "scheduled",
        ),
    )
    await conn.commit()
    return cursor.lastrowid


async def log_confidence_change(conn: aiosqlite.Connection, data: dict):
    await conn.execute(
        """INSERT INTO confidence_log
        (week_start, old_threshold, new_threshold, thumbs_up_count,
         thumbs_down_count, uncertain_count, reasoning)
        VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (
            data["week_start"], data["old_threshold"], data["new_threshold"],
            data.get("thumbs_up_count"), data.get("thumbs_down_count"),
            data.get("uncertain_count"), data.get("reasoning"),
        ),
    )
    await conn.commit()


async def get_rated_interactions_since(conn: aiosqlite.Connection, since: str) -> list[dict]:
    cursor = await conn.execute(
        "SELECT * FROM interactions WHERE human_rating IS NOT NULL AND created_at > ? ORDER BY created_at DESC",
        (since,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_stale_interactions(conn: aiosqlite.Connection, days: int) -> list[dict]:
    """Interactions sent >N days ago with no outcome and no newer interaction for same lead."""
    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
    cursor = await conn.execute(
        """SELECT i.* FROM interactions i
        WHERE i.was_sent = 1 AND i.outcome IS NULL AND i.created_at < ?
        AND NOT EXISTS (
            SELECT 1 FROM interactions i2
            WHERE i2.lead_email = i.lead_email AND i2.campaign_id = i.campaign_id
            AND i2.created_at > i.created_at
        )""",
        (cutoff,),
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_replied_again_missing(conn: aiosqlite.Connection) -> list[dict]:
    """Interactions where a newer interaction exists but outcome not set."""
    cursor = await conn.execute(
        """SELECT i.* FROM interactions i
        WHERE i.was_sent = 1 AND i.outcome IS NULL
        AND EXISTS (
            SELECT 1 FROM interactions i2
            WHERE i2.lead_email = i.lead_email AND i2.campaign_id = i.campaign_id
            AND i2.created_at > i.created_at
        )"""
    )
    return [dict(r) for r in await cursor.fetchall()]


async def get_weekly_stats(conn: aiosqlite.Connection, since: str) -> dict:
    """Get stats for weekly digest."""
    cursor = await conn.execute(
        "SELECT classification, COUNT(*) as cnt FROM interactions WHERE created_at > ? GROUP BY classification",
        (since,),
    )
    categories = {row["classification"]: row["cnt"] for row in await cursor.fetchall()}

    cursor = await conn.execute(
        "SELECT COUNT(*) as cnt FROM meetings WHERE created_at > ?", (since,)
    )
    meetings_count = (await cursor.fetchone())["cnt"]

    cursor = await conn.execute(
        "SELECT human_rating, COUNT(*) as cnt FROM interactions WHERE human_rating IS NOT NULL AND created_at > ? GROUP BY human_rating",
        (since,),
    )
    ratings = {row["human_rating"]: row["cnt"] for row in await cursor.fetchall()}

    cursor = await conn.execute(
        "SELECT COUNT(*) as cnt FROM interactions WHERE human_override_text IS NOT NULL AND created_at > ?",
        (since,),
    )
    override_count = (await cursor.fetchone())["cnt"]

    return {
        "categories": categories,
        "total": sum(categories.values()),
        "meetings_count": meetings_count,
        "thumbs_up": ratings.get("thumbs_up", 0),
        "thumbs_down": ratings.get("thumbs_down", 0),
        "override_count": override_count,
    }
