import pytest
import pytest_asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch
from db.database import init_db
from webhooks.instantly_webhook import handle_instantly_webhook

FIXTURES = Path(__file__).parent / "fixtures"


@pytest_asyncio.fixture
async def db(tmp_path):
    conn = await init_db(tmp_path / "test.db")
    yield conn
    await conn.close()


@pytest.fixture
def mock_clients():
    return {
        "gleadsy": {
            "client_id": "gleadsy", "client_name": "Gleadsy",
            "campaigns": ["test-campaign-uuid"],
            "company_description": "Digital marketing", "service_offering": "Cold email",
            "value_proposition": "5 susitikimai", "pricing": "Individualios",
            "target_audience": "B2B", "meeting": {
                "participant_from_client": "Paulius", "purpose": "Konsultacija",
                "duration_minutes": 30, "google_calendar_id": "primary",
                "working_hours": {"start": "09:00", "end": "17:00", "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]},
                "buffer_minutes": 15, "advance_days": 7, "slots_to_offer": 3,
            },
            "faq": [{"question": "Kiek kainuoja?", "answer": "Individualios kainos"}],
            "boundaries": {"cannot_promise": [], "escalate_topics": []},
            "tone": {"formality": "semi-formal", "addressing": "Jūs", "language": "lt",
                     "personality": "Draugiškas", "max_reply_length_sentences": 5,
                     "sign_off": "Pagarbiai", "sender_name": "Paulius"},
        }
    }


def _load_fixture(name: str) -> dict:
    with open(FIXTURES / name, "r") as f:
        return json.load(f)


@pytest.mark.asyncio
async def test_ignores_non_reply_event(db, mock_clients):
    payload = {"event_type": "email_sent", "email_id": "x"}
    result = await handle_instantly_webhook(payload, db, mock_clients, 0.7)
    assert result["status"] == "ignored"


@pytest.mark.asyncio
async def test_ignores_duplicate(db, mock_clients):
    payload = _load_fixture("reply_interested.json")
    with patch("webhooks.instantly_webhook.classify_reply") as mock_cls, \
         patch("webhooks.instantly_webhook.generate_reply", return_value="Reply"), \
         patch("webhooks.instantly_webhook.send_reply", return_value={}), \
         patch("webhooks.instantly_webhook.get_free_slots", return_value=[]), \
         patch("webhooks.instantly_webhook.notify_reply_sent"):
        mock_cls.return_value = AsyncMock(category="INTERESTED", confidence=0.9, reasoning="Test")
        mock_cls.return_value.category = "INTERESTED"
        mock_cls.return_value.confidence = 0.9
        mock_cls.return_value.reasoning = "Test"
        await handle_instantly_webhook(payload, db, mock_clients, 0.7)
    # Second time = duplicate
    result = await handle_instantly_webhook(payload, db, mock_clients, 0.7)
    assert result["status"] == "ignored"
    assert result["reason"] == "duplicate"


@pytest.mark.asyncio
async def test_unknown_campaign_escalates(db, mock_clients):
    payload = _load_fixture("reply_interested.json")
    payload["campaign_id"] = "unknown-uuid"
    with patch("webhooks.instantly_webhook.notify_unknown_campaign"):
        result = await handle_instantly_webhook(payload, db, mock_clients, 0.7)
    assert result["status"] == "error"
    assert result["reason"] == "unknown campaign"


@pytest.mark.asyncio
async def test_unsubscribe_logs_no_reply(db, mock_clients):
    payload = _load_fixture("reply_unsubscribe.json")
    with patch("webhooks.instantly_webhook.classify_reply") as mock_cls:
        mock_cls.return_value = AsyncMock(category="UNSUBSCRIBE", confidence=0.95, reasoning="Nedomina")
        mock_cls.return_value.category = "UNSUBSCRIBE"
        mock_cls.return_value.confidence = 0.95
        mock_cls.return_value.reasoning = "Nedomina"
        result = await handle_instantly_webhook(payload, db, mock_clients, 0.7)
    assert result["status"] == "logged"
    assert result["reason"] == "unsubscribe"


@pytest.mark.asyncio
async def test_interested_first_time_sends_reply_with_slots(db, mock_clients):
    payload = _load_fixture("reply_interested.json")
    mock_slots = [{"date": "2026-04-02", "day_name": "trečiadienį", "time": "10:00", "end": "10:30", "iso": "2026-04-02T10:00:00+03:00"}]

    from core.quality_reviewer import QualityResult
    with patch("webhooks.instantly_webhook.classify_reply") as mock_cls, \
         patch("webhooks.instantly_webhook.generate_reply", return_value="Puiku! Siūlau trečiadienį 10:00.") as mock_gen, \
         patch("webhooks.instantly_webhook.review_quality", return_value=QualityResult(score=9, passed=True, issues=[], summary="ok")), \
         patch("webhooks.instantly_webhook.send_reply", return_value={}) as mock_send, \
         patch("webhooks.instantly_webhook.get_free_slots", return_value=mock_slots), \
         patch("webhooks.instantly_webhook.notify_reply_sent"):
        mock_cls.return_value = AsyncMock(category="INTERESTED", confidence=0.92, reasoning="Nori susitikti")
        mock_cls.return_value.category = "INTERESTED"
        mock_cls.return_value.confidence = 0.92
        mock_cls.return_value.reasoning = "Nori susitikti"
        result = await handle_instantly_webhook(payload, db, mock_clients, 0.7)

    assert result["status"] == "sent"
    mock_send.assert_called_once()
    mock_gen.assert_called_once()


@pytest.mark.asyncio
async def test_question_sends_reply(db, mock_clients):
    payload = _load_fixture("reply_question.json")
    from core.quality_reviewer import QualityResult
    with patch("webhooks.instantly_webhook.classify_reply") as mock_cls, \
         patch("webhooks.instantly_webhook.match_faq", return_value={"faq_index": 0, "confidence": 0.9, "adapted_answer": "Kainos individualios."}), \
         patch("webhooks.instantly_webhook.generate_reply", return_value="Kainos priklauso nuo poreikių.") as mock_gen, \
         patch("webhooks.instantly_webhook.review_quality", return_value=QualityResult(score=9, passed=True, issues=[], summary="ok")), \
         patch("webhooks.instantly_webhook.send_reply", return_value={}), \
         patch("webhooks.instantly_webhook.notify_reply_sent"):
        mock_cls.return_value = AsyncMock(category="QUESTION", confidence=0.88, reasoning="Klausia apie kainą")
        mock_cls.return_value.category = "QUESTION"
        mock_cls.return_value.confidence = 0.88
        mock_cls.return_value.reasoning = "Klausia apie kainą"
        result = await handle_instantly_webhook(payload, db, mock_clients, 0.7)

    assert result["status"] == "sent"


@pytest.mark.asyncio
async def test_low_confidence_escalates(db, mock_clients):
    payload = _load_fixture("reply_interested.json")
    payload["email_id"] = "low-conf-uuid"
    with patch("webhooks.instantly_webhook.classify_reply") as mock_cls, \
         patch("webhooks.instantly_webhook.notify_escalation"):
        mock_cls.return_value = AsyncMock(category="INTERESTED", confidence=0.5, reasoning="Neaišku")
        mock_cls.return_value.category = "INTERESTED"
        mock_cls.return_value.confidence = 0.5
        mock_cls.return_value.reasoning = "Neaišku"
        result = await handle_instantly_webhook(payload, db, mock_clients, 0.7)

    assert result["status"] == "escalated"
    assert result["reason"] == "uncertain"


@pytest.mark.asyncio
async def test_cooldown_blocks_reply(db, mock_clients):
    # First reply goes through
    payload1 = _load_fixture("reply_interested.json")
    from core.quality_reviewer import QualityResult
    with patch("webhooks.instantly_webhook.classify_reply") as mock_cls, \
         patch("webhooks.instantly_webhook.generate_reply", return_value="Reply"), \
         patch("webhooks.instantly_webhook.review_quality", return_value=QualityResult(score=9, passed=True, issues=[], summary="ok")), \
         patch("webhooks.instantly_webhook.send_reply", return_value={}), \
         patch("webhooks.instantly_webhook.get_free_slots", return_value=[]), \
         patch("webhooks.instantly_webhook.notify_reply_sent"):
        mock_cls.return_value = AsyncMock(category="INTERESTED", confidence=0.9, reasoning="Test")
        mock_cls.return_value.category = "INTERESTED"
        mock_cls.return_value.confidence = 0.9
        mock_cls.return_value.reasoning = "Test"
        await handle_instantly_webhook(payload1, db, mock_clients, 0.7)

    # Second reply within cooldown should be blocked
    payload2 = _load_fixture("reply_interested.json")
    payload2["email_id"] = "cooldown-test-uuid"
    result = await handle_instantly_webhook(payload2, db, mock_clients, 0.7)
    assert result["status"] == "ignored"
    assert result["reason"] == "cooldown active"


@pytest.fixture
def fr_clients():
    return {
        "gleadsy_fr": {
            "client_id": "gleadsy_fr", "client_name": "Gleadsy FR",
            "approval_required": True,
            "campaigns": [
                {"id": "campaign-fr-uuid", "language": "fr", "name": "FR outreach"},
            ],
            "company_description": "Digital marketing", "service_offering": "Cold email",
            "value_proposition": "5 rendez-vous qualifiés", "pricing": "800€/mois",
            "target_audience": "B2B",
            "meeting": {
                "participant_from_client": "Paulius", "purpose": "Consultation",
                "duration_minutes": 30, "google_calendar_id": "primary",
                "working_hours": {"start": "09:00", "end": "17:00",
                                   "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]},
                "buffer_minutes": 15, "advance_days": 7, "slots_to_offer": 3,
            },
            "faq": [{"question": "Prix?", "answer": "Individuel"}],
            "boundaries": {"cannot_promise": [], "escalate_topics": []},
            "tone": {"formality": "semi-formal", "addressing": "vous", "language": "fr",
                     "personality": "Professional", "max_reply_length_sentences": 5,
                     "sign_off": "Cordialement", "sender_name": "Paulius"},
        }
    }


@pytest.mark.asyncio
async def test_fr_reply_goes_to_pending_queue(db, fr_clients):
    payload = _load_fixture("reply_fr_interested.json")
    with patch("webhooks.instantly_webhook.classify_reply") as mock_cls, \
         patch("webhooks.instantly_webhook.generate_reply",
               new=AsyncMock(return_value="Merci pour votre intérêt.")), \
         patch("webhooks.instantly_webhook.review_quality") as mock_qr, \
         patch("webhooks.instantly_webhook.translate_to_lt",
               new=AsyncMock(side_effect=["Labas, įdomu.", "Ačiū už susidomėjimą."])), \
         patch("webhooks.instantly_webhook.send_reply", new=AsyncMock()) as mock_send, \
         patch("webhooks.instantly_webhook.notify_approval_pending",
               new=AsyncMock()) as mock_notify, \
         patch("webhooks.instantly_webhook.get_free_slots", new=AsyncMock(return_value=[])):
        mock_cls.return_value = type("C", (), {
            "category": "INTERESTED", "confidence": 0.92, "reasoning": "wants pricing",
        })()
        mock_qr.return_value = type("Q", (), {
            "passed": True, "score": 8, "issues": [], "summary": "Good",
            "improvement_suggestion": "",
        })()
        result = await handle_instantly_webhook(payload, db, fr_clients, 0.5)

    assert result["status"] == "pending_approval"
    mock_send.assert_not_called()  # Should NOT auto-send
    mock_notify.assert_called_once()

    cursor = await db.execute(
        "SELECT approval_status, original_language, prospect_message_lt, agent_reply_lt, was_sent "
        "FROM interactions WHERE email_id = ?",
        (payload["email_id"],),
    )
    row = dict(await cursor.fetchone())
    assert row["approval_status"] == "pending"
    assert row["was_sent"] == 0
    assert row["original_language"] == "fr"
    assert row["prospect_message_lt"] == "Labas, įdomu."
    assert row["agent_reply_lt"] == "Ačiū už susidomėjimą."


@pytest.mark.asyncio
async def test_lt_reply_still_auto_sends(db, mock_clients):
    # LT client without approval_required - same flow as before (no pending queue)
    payload = _load_fixture("reply_interested.json")
    with patch("webhooks.instantly_webhook.classify_reply") as mock_cls, \
         patch("webhooks.instantly_webhook.generate_reply",
               new=AsyncMock(return_value="Ačiū už susidomėjimą")), \
         patch("webhooks.instantly_webhook.review_quality") as mock_qr, \
         patch("webhooks.instantly_webhook.send_reply", new=AsyncMock()) as mock_send, \
         patch("webhooks.instantly_webhook.notify_approval_pending",
               new=AsyncMock()) as mock_notify, \
         patch("webhooks.instantly_webhook.notify_reply_sent", new=AsyncMock()), \
         patch("webhooks.instantly_webhook.get_free_slots", new=AsyncMock(return_value=[])):
        mock_cls.return_value = type("C", (), {
            "category": "INTERESTED", "confidence": 0.92, "reasoning": "test",
        })()
        mock_qr.return_value = type("Q", (), {
            "passed": True, "score": 9, "issues": [], "summary": "Good",
            "improvement_suggestion": "",
        })()
        result = await handle_instantly_webhook(payload, db, mock_clients, 0.5)

    assert result["status"] == "sent"
    mock_send.assert_called_once()
    mock_notify.assert_not_called()
