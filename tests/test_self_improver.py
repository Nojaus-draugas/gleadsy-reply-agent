import pytest
import pytest_asyncio
from db.database import init_db, log_interaction, update_rating, update_outcome
from core.self_improver import get_best_examples, get_anti_patterns


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    # Seed data: 3 good examples, 1 bad
    for i in range(3):
        iid = await log_interaction(conn, {
            "campaign_id": "camp-1", "campaign_name": "Test",
            "lead_email": f"good{i}@test.com", "email_account": "sender@test.com",
            "email_id": f"good-{i}", "client_id": "gleadsy",
            "prospect_message": f"Domina {i}", "classification": "INTERESTED",
            "confidence": 0.9, "agent_reply": f"Puiku! Siūlau laikus {i}",
            "was_sent": True, "thread_position": 1,
        })
        await update_rating(conn, iid, "thumbs_up", None, None)
        if i == 0:
            await update_outcome(conn, iid, "meeting_booked")

    bad_id = await log_interaction(conn, {
        "campaign_id": "camp-1", "campaign_name": "Test",
        "lead_email": "bad@test.com", "email_account": "sender@test.com",
        "email_id": "bad-1", "client_id": "gleadsy",
        "prospect_message": "Kiek kainuoja?", "classification": "QUESTION",
        "confidence": 0.8, "agent_reply": "Kainuoja 500 eur",
        "was_sent": True, "thread_position": 1,
    })
    await update_rating(conn, bad_id, "thumbs_down", "Aptarsime per pokalbį", "Per tiesiai apie kainą")

    yield conn
    await conn.close()


@pytest.mark.asyncio
async def test_get_best_examples(db):
    examples = await get_best_examples(db, "INTERESTED", "gleadsy", limit=3)
    assert len(examples) <= 3
    assert all(e["agent_reply"] for e in examples)
    # First should be the one with meeting_booked + thumbs_up
    assert examples[0]["outcome"] == "meeting_booked" or examples[0]["human_rating"] == "thumbs_up"


@pytest.mark.asyncio
async def test_get_best_examples_empty_category(db):
    examples = await get_best_examples(db, "REFERRAL", "gleadsy", limit=3)
    assert examples == []


@pytest.mark.asyncio
async def test_get_anti_patterns(db):
    patterns = await get_anti_patterns(db, "QUESTION", "gleadsy", limit=2)
    assert len(patterns) == 1
    assert patterns[0]["bad_reply"] == "Kainuoja 500 eur"
    assert patterns[0]["correct_reply"] == "Aptarsime per pokalbį"
