import pytest
import pytest_asyncio
import json
from db.database import init_db


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_interactions_table_has_new_columns(db):
    cursor = await db.execute("PRAGMA table_info(interactions)")
    cols = {row["name"] for row in await cursor.fetchall()}
    expected_new = {
        "original_language",
        "prospect_message_lt",
        "agent_reply_lt",
        "approval_status",
        "approved_at",
        "approved_by",
        "edit_history",
        "final_sent_text",
    }
    missing = expected_new - cols
    assert not missing, f"Missing columns: {missing}"


@pytest.mark.asyncio
async def test_approval_index_exists(db):
    cursor = await db.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND name='idx_interactions_approval'"
    )
    assert await cursor.fetchone() is not None


from datetime import datetime
from db.database import (
    log_interaction, update_approval_status, get_pending_drafts,
    get_pending_count, append_edit_history, update_draft_text,
    get_thread_reply_count, atomically_claim_for_approval,
    restore_pending_after_failed_send,
)


async def _log_pending(db, **overrides) -> int:
    base = {
        "campaign_id": "camp-1", "lead_email": "x@y.com",
        "email_id": overrides.pop("email_id", f"eid-{datetime.utcnow().timestamp()}"),
        "client_id": "gleadsy",
        "prospect_message": "Bonjour", "classification": "QUESTION",
        "confidence": 0.9, "approval_status": "pending", "was_sent": False,
    }
    base.update(overrides)
    return await log_interaction(db, base)


@pytest.mark.asyncio
async def test_update_approval_status_to_sent(db):
    iid = await _log_pending(db)
    await update_approval_status(db, iid, "sent", approved_by="paulius",
                                  final_sent_text="Merci")
    cursor = await db.execute(
        "SELECT approval_status, approved_by, approved_at, was_sent, final_sent_text "
        "FROM interactions WHERE id = ?", (iid,),
    )
    row = dict(await cursor.fetchone())
    assert row["approval_status"] == "sent"
    assert row["approved_by"] == "paulius"
    assert row["approved_at"] is not None
    assert row["was_sent"] == 1  # SQLite BOOLEAN -> int
    assert row["final_sent_text"] == "Merci"


@pytest.mark.asyncio
async def test_update_approval_status_reject_does_not_flip_was_sent(db):
    iid = await _log_pending(db)
    await update_approval_status(db, iid, "rejected", approved_by="paulius")
    cursor = await db.execute(
        "SELECT approval_status, was_sent FROM interactions WHERE id = ?", (iid,),
    )
    row = dict(await cursor.fetchone())
    assert row["approval_status"] == "rejected"
    assert row["was_sent"] == 0


@pytest.mark.asyncio
async def test_get_pending_drafts_returns_only_pending(db):
    pending_iid = await _log_pending(db, email_id="eid-a")
    await _log_pending(db, email_id="eid-b", approval_status="sent", was_sent=True)
    rejected_iid = await _log_pending(db, email_id="eid-c", approval_status="rejected")
    pending = await get_pending_drafts(db)
    ids = {row["id"] for row in pending}
    assert pending_iid in ids
    assert rejected_iid not in ids
    assert len(ids) == 1


@pytest.mark.asyncio
async def test_get_pending_count(db):
    await _log_pending(db, email_id="eid-1")
    await _log_pending(db, email_id="eid-2")
    await _log_pending(db, email_id="eid-3", approval_status="sent", was_sent=True)
    assert await get_pending_count(db) == 2


@pytest.mark.asyncio
async def test_get_pending_drafts_filtered_by_client(db):
    iid_a = await _log_pending(db, email_id="eid-a", client_id="gleadsy")
    iid_b = await _log_pending(db, email_id="eid-b", client_id="ibjoist")
    filtered = await get_pending_drafts(db, client_id="gleadsy")
    assert {row["id"] for row in filtered} == {iid_a}


@pytest.mark.asyncio
async def test_append_edit_history_creates_list_on_first_edit(db):
    iid = await _log_pending(db)
    await append_edit_history(db, iid, {
        "lt_instruction": "pridek klausima",
        "before": "Merci",
        "after": "Merci! Une question?",
    })
    cursor = await db.execute("SELECT edit_history FROM interactions WHERE id = ?", (iid,))
    raw = (await cursor.fetchone())["edit_history"]
    history = json.loads(raw)
    assert len(history) == 1
    assert history[0]["lt_instruction"] == "pridek klausima"
    assert "ts" in history[0]


@pytest.mark.asyncio
async def test_append_edit_history_appends(db):
    iid = await _log_pending(db)
    await append_edit_history(db, iid, {"lt_instruction": "a", "before": "x", "after": "y"})
    await append_edit_history(db, iid, {"lt_instruction": "b", "before": "y", "after": "z"})
    cursor = await db.execute("SELECT edit_history FROM interactions WHERE id = ?", (iid,))
    history = json.loads((await cursor.fetchone())["edit_history"])
    assert len(history) == 2
    assert history[0]["lt_instruction"] == "a"
    assert history[1]["lt_instruction"] == "b"


@pytest.mark.asyncio
async def test_update_draft_text(db):
    iid = await _log_pending(db, agent_reply="Merci", agent_reply_lt="Aciu")
    await update_draft_text(db, iid, "Merci beaucoup", "Labai aciu")
    cursor = await db.execute(
        "SELECT agent_reply, agent_reply_lt FROM interactions WHERE id = ?", (iid,),
    )
    row = dict(await cursor.fetchone())
    assert row["agent_reply"] == "Merci beaucoup"
    assert row["agent_reply_lt"] == "Labai aciu"


@pytest.mark.asyncio
async def test_thread_reply_count_includes_sent_approval_status(db):
    # Sent via approval should also count toward thread max
    await _log_pending(db, email_id="eid-1", approval_status="sent", was_sent=True)
    await _log_pending(db, email_id="eid-2", approval_status="sent_manually", was_sent=True)
    # Pending shouldn't count (draft not actually sent to lead)
    await _log_pending(db, email_id="eid-3", approval_status="pending", was_sent=False)
    count = await get_thread_reply_count(db, "x@y.com", "camp-1")
    assert count == 2


@pytest.mark.asyncio
async def test_atomic_claim_succeeds_once(db):
    iid = await _log_pending(db, email_id="eid-race")
    # First caller wins
    assert await atomically_claim_for_approval(db, iid) is True
    # Verify state transitioned to 'approving'
    cursor = await db.execute(
        "SELECT approval_status FROM interactions WHERE id = ?", (iid,)
    )
    assert (await cursor.fetchone())["approval_status"] == "approving"


@pytest.mark.asyncio
async def test_atomic_claim_second_caller_loses(db):
    iid = await _log_pending(db, email_id="eid-race2")
    assert await atomically_claim_for_approval(db, iid) is True
    # Second caller cannot claim - already in 'approving'
    assert await atomically_claim_for_approval(db, iid) is False


@pytest.mark.asyncio
async def test_atomic_claim_fails_on_rejected_draft(db):
    iid = await _log_pending(db, email_id="eid-race3")
    await update_approval_status(db, iid, "rejected", approved_by="paulius")
    # Can't claim a rejected draft
    assert await atomically_claim_for_approval(db, iid) is False


@pytest.mark.asyncio
async def test_restore_pending_after_failed_send(db):
    iid = await _log_pending(db, email_id="eid-restore")
    await atomically_claim_for_approval(db, iid)
    await restore_pending_after_failed_send(db, iid)
    cursor = await db.execute(
        "SELECT approval_status FROM interactions WHERE id = ?", (iid,)
    )
    assert (await cursor.fetchone())["approval_status"] == "pending"
    # Now claimable again
    assert await atomically_claim_for_approval(db, iid) is True


@pytest.mark.asyncio
async def test_log_interaction_stores_reply_subject(db):
    iid = await _log_pending(db, email_id="eid-subj", reply_subject="Inquiry about pricing")
    cursor = await db.execute(
        "SELECT reply_subject FROM interactions WHERE id = ?", (iid,)
    )
    row = await cursor.fetchone()
    assert row["reply_subject"] == "Inquiry about pricing"


@pytest.mark.asyncio
async def test_log_interaction_reply_subject_defaults_to_none(db):
    iid = await _log_pending(db, email_id="eid-nosubj")
    cursor = await db.execute(
        "SELECT reply_subject FROM interactions WHERE id = ?", (iid,)
    )
    row = await cursor.fetchone()
    assert row["reply_subject"] is None
