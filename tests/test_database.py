import pytest
import pytest_asyncio
from db.database import init_db, log_interaction, is_duplicate, get_thread_reply_count, reply_sent_within_cooldown, update_rating, get_interactions_for_lead, is_human_takeover, set_human_takeover, update_outcome


@pytest_asyncio.fixture
async def db(tmp_path):
    db_path = tmp_path / "test.db"
    conn = await init_db(db_path)
    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_log_and_retrieve_interaction(db):
    interaction_id = await log_interaction(db, {
        "campaign_id": "camp-1",
        "campaign_name": "Test Campaign",
        "lead_email": "test@example.com",
        "email_account": "sender@gleadsy.lt",
        "email_id": "email-uuid-1",
        "client_id": "gleadsy",
        "prospect_message": "Sveiki, domina",
        "classification": "INTERESTED",
        "confidence": 0.95,
        "classification_reasoning": "Aiškiai domisi",
        "agent_reply": "Puiku! Siūlau laikus...",
        "was_sent": True,
        "thread_position": 1,
    })
    assert interaction_id == 1
    interactions = await get_interactions_for_lead(db, "test@example.com", "camp-1")
    assert len(interactions) == 1
    assert interactions[0]["classification"] == "INTERESTED"


@pytest.mark.asyncio
async def test_duplicate_detection(db):
    await log_interaction(db, {
        "campaign_id": "camp-1", "campaign_name": "Test",
        "lead_email": "test@example.com", "email_account": "sender@gleadsy.lt",
        "email_id": "email-uuid-1", "client_id": "gleadsy",
        "prospect_message": "Test", "classification": "INTERESTED",
        "confidence": 0.9, "was_sent": True, "thread_position": 1,
    })
    assert await is_duplicate(db, "email-uuid-1") is True
    assert await is_duplicate(db, "email-uuid-2") is False


@pytest.mark.asyncio
async def test_thread_reply_count(db):
    for i in range(3):
        await log_interaction(db, {
            "campaign_id": "camp-1", "campaign_name": "Test",
            "lead_email": "test@example.com", "email_account": "sender@gleadsy.lt",
            "email_id": f"email-{i}", "client_id": "gleadsy",
            "prospect_message": f"Msg {i}", "classification": "QUESTION",
            "confidence": 0.8, "was_sent": True, "thread_position": i + 1,
        })
    count = await get_thread_reply_count(db, "test@example.com", "camp-1")
    assert count == 3


@pytest.mark.asyncio
async def test_cooldown_check(db):
    await log_interaction(db, {
        "campaign_id": "camp-1", "campaign_name": "Test",
        "lead_email": "test@example.com", "email_account": "sender@gleadsy.lt",
        "email_id": "email-1", "client_id": "gleadsy",
        "prospect_message": "Test", "classification": "INTERESTED",
        "confidence": 0.9, "was_sent": True, "thread_position": 1,
    })
    assert await reply_sent_within_cooldown(db, "test@example.com", "camp-1", hours=4) is True
    assert await reply_sent_within_cooldown(db, "other@example.com", "camp-1", hours=4) is False


@pytest.mark.asyncio
async def test_update_rating(db):
    iid = await log_interaction(db, {
        "campaign_id": "camp-1", "campaign_name": "Test",
        "lead_email": "test@example.com", "email_account": "sender@gleadsy.lt",
        "email_id": "email-1", "client_id": "gleadsy",
        "prospect_message": "Test", "classification": "QUESTION",
        "confidence": 0.85, "was_sent": True, "thread_position": 1,
    })
    await update_rating(db, iid, "thumbs_up", None, None)
    interactions = await get_interactions_for_lead(db, "test@example.com", "camp-1")
    assert interactions[0]["human_rating"] == "thumbs_up"


@pytest.mark.asyncio
async def test_human_takeover(db):
    assert await is_human_takeover(db, "test@example.com", "camp-1") is False
    await set_human_takeover(db, "test@example.com", "camp-1")
    assert await is_human_takeover(db, "test@example.com", "camp-1") is True


@pytest.mark.asyncio
async def test_update_outcome(db):
    iid = await log_interaction(db, {
        "campaign_id": "camp-1", "campaign_name": "Test",
        "lead_email": "test@example.com", "email_account": "sender@gleadsy.lt",
        "email_id": "email-1", "client_id": "gleadsy",
        "prospect_message": "Test", "classification": "INTERESTED",
        "confidence": 0.9, "was_sent": True, "thread_position": 1,
    })
    await update_outcome(db, iid, "meeting_booked")
    interactions = await get_interactions_for_lead(db, "test@example.com", "camp-1")
    assert interactions[0]["outcome"] == "meeting_booked"
