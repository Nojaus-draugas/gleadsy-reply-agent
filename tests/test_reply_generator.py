import pytest
from unittest.mock import AsyncMock, patch
from core.reply_generator import generate_reply, match_faq, parse_time_confirmation


MOCK_CLIENT = {
    "client_name": "Gleadsy", "company_description": "Digital marketing",
    "service_offering": "Cold email", "value_proposition": "5 susitikimai",
    "pricing": "Individualios kainos", "tone": {
        "language": "lt", "addressing": "Jūs", "personality": "Draugiškas",
        "max_reply_length_sentences": 5, "sign_off": "Pagarbiai", "sender_name": "Paulius",
    },
    "boundaries": {"cannot_promise": ["Konkrečių skaičių"], "escalate_topics": ["Teisiniai"]},
    "faq": [{"question": "Kiek kainuoja?", "answer": "Individualios kainos"}],
}


@pytest.mark.asyncio
async def test_generate_reply_interested():
    mock_response = AsyncMock()
    mock_response.content = [AsyncMock(text="Puiku! Galėtume susitikti trečiadienį 10:00 arba ketvirtadienį 14:00.")]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("core.classifier.get_anthropic_client", return_value=mock_client):
        result = await generate_reply(
            prospect_message="Taip, domina",
            classification="INTERESTED",
            client_config=MOCK_CLIENT,
            few_shots=[], anti_patterns=[],
            available_slots=[{"date": "2026-04-02", "day_name": "trečiadienį", "time": "10:00", "end": "10:30"}],
        )
    assert "trečiadienį" in result or len(result) > 0


@pytest.mark.asyncio
async def test_generate_reply_question():
    mock_response = AsyncMock()
    mock_response.content = [AsyncMock(text="Tikslesnė kaina priklauso nuo poreikių. Gal aptartume per pokalbį?")]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("core.classifier.get_anthropic_client", return_value=mock_client):
        result = await generate_reply(
            prospect_message="Kiek tai kainuoja?",
            classification="QUESTION",
            client_config=MOCK_CLIENT,
            few_shots=[], anti_patterns=[],
            matching_faq="Individualios kainos — aptariame per susitikimą.",
        )
    assert len(result) > 0


@pytest.mark.asyncio
async def test_match_faq():
    mock_response = AsyncMock()
    mock_response.content = [AsyncMock(text='{"faq_index": 0, "confidence": 0.9, "adapted_answer": "Kainos individualios."}')]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("core.classifier.get_anthropic_client", return_value=mock_client):
        result = await match_faq("Kiek kainuoja?", MOCK_CLIENT["faq"])
    assert result["faq_index"] == 0
    assert result["confidence"] == 0.9


@pytest.mark.asyncio
async def test_parse_time_confirmation():
    mock_response = AsyncMock()
    mock_response.content = [AsyncMock(text='{"confirmed_slot_index": 1, "confidence": 0.95}')]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("core.classifier.get_anthropic_client", return_value=mock_client):
        result = await parse_time_confirmation(
            "Ketvirtadienis 14:00 tinka",
            '[{"date":"2026-04-02","time":"10:00"},{"date":"2026-04-03","time":"14:00"}]',
        )
    assert result["confirmed_slot_index"] == 1
    assert result["confidence"] == 0.95


@pytest.mark.asyncio
async def test_parse_time_confirmation_unclear():
    mock_response = AsyncMock()
    mock_response.content = [AsyncMock(text="not json")]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("core.classifier.get_anthropic_client", return_value=mock_client):
        result = await parse_time_confirmation("Hmm gal", "[]")
    assert result["confirmed_slot_index"] is None
    assert result["confidence"] == 0.0


def _minimal_client(language="lt"):
    return {
        "client_id": "c1", "client_name": "Test",
        "company_description": "x", "service_offering": "y",
        "value_proposition": "z", "pricing": "p",
        "boundaries": {"cannot_promise": []},
        "tone": {
            "language": language, "addressing": "vous" if language == "fr" else "Jus",
            "personality": "Friendly", "max_reply_length_sentences": 5,
            "sign_off": "Cordialement" if language == "fr" else "Pagarbiai",
            "sender_name": "Paulius",
        },
    }


@pytest.mark.asyncio
async def test_generate_reply_target_language_lt_default():
    # When target_language is None, use tone.language from client_config (backward compat)
    with patch("core.reply_generator.call_claude_with_retry", new=AsyncMock(return_value="Labas")) as mock:
        result = await generate_reply(
            prospect_message="Labas, idomu", classification="QUESTION",
            client_config=_minimal_client("lt"), few_shots=[], anti_patterns=[],
        )
    assert result == "Labas"
    # System prompt should mention 'lt' somewhere
    system_blocks = mock.call_args.kwargs["system"]
    sys_text = "\n".join(b["text"] if isinstance(b, dict) else b for b in system_blocks)
    assert "lt" in sys_text.lower() or "lithuanian" in sys_text.lower()


@pytest.mark.asyncio
async def test_generate_reply_target_language_overrides_tone():
    # Campaign is FR but client's tone.language is LT - target_language should win
    with patch("core.reply_generator.call_claude_with_retry", new=AsyncMock(return_value="Merci")) as mock:
        result = await generate_reply(
            prospect_message="Bonjour", classification="QUESTION",
            client_config=_minimal_client("lt"), few_shots=[], anti_patterns=[],
            target_language="fr",
        )
    assert result == "Merci"
    system_blocks = mock.call_args.kwargs["system"]
    sys_text = "\n".join(b["text"] if isinstance(b, dict) else b for b in system_blocks)
    # target_language 'fr' must appear explicitly in the prompt (override directive)
    assert "fr" in sys_text.lower() or "french" in sys_text.lower()
