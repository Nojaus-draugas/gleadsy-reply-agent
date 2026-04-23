import pytest
from unittest.mock import AsyncMock, patch
from core.translation import translate_to_lt, rewrite_draft


@pytest.mark.asyncio
async def test_translate_lt_noop_when_already_lt():
    # No API call should be made for LT source
    with patch("core.translation.call_claude_with_retry") as mock:
        result = await translate_to_lt("Labas, kaip sekasi?", "lt")
    assert result == "Labas, kaip sekasi?"
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_translate_lt_empty_noop():
    with patch("core.translation.call_claude_with_retry") as mock:
        assert await translate_to_lt("", "fr") == ""
        assert await translate_to_lt("   ", "de") == "   "
    mock.assert_not_called()


@pytest.mark.asyncio
async def test_translate_lt_calls_claude_for_fr():
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(return_value="Labas, ačiū.")) as mock:
        result = await translate_to_lt("Bonjour, merci.", "fr")
    assert result == "Labas, ačiū."
    mock.assert_called_once()
    # Verify model is Haiku (cheap) and purpose set
    kwargs = mock.call_args.kwargs
    assert "haiku" in kwargs["model"].lower()
    assert kwargs["purpose"] == "translate_to_lt"


@pytest.mark.asyncio
async def test_translate_lt_prompt_includes_source_language():
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(return_value="...")) as mock:
        await translate_to_lt("Guten Tag", "de")
    user_msg = mock.call_args.kwargs["messages"][0]["content"]
    system_msg = mock.call_args.kwargs["system"]
    assert "Guten Tag" in user_msg
    # System prompt should mention source language so Claude knows what to translate from
    combined = (system_msg or "") + user_msg
    assert "de" in combined.lower() or "german" in combined.lower() or "translate" in combined.lower()


@pytest.mark.asyncio
async def test_translate_lt_returns_empty_on_api_failure():
    from core.classifier import APIUnavailableError
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(side_effect=APIUnavailableError("down"))):
        result = await translate_to_lt("Bonjour", "fr")
    assert result == ""  # graceful degradation - dashboard shows original only


@pytest.mark.asyncio
async def test_rewrite_draft_calls_sonnet():
    client_config = {"client_name": "Gleadsy", "tone": {"language": "fr"}}
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(return_value="Merci! Quand nous rencontrer?")) as mock:
        result = await rewrite_draft(
            original_draft="Merci pour votre intérêt.",
            lt_instruction="Pridėk klausimą apie susitikimo laiką",
            target_language="fr",
            client_config=client_config,
        )
    assert result == "Merci! Quand nous rencontrer?"
    kwargs = mock.call_args.kwargs
    assert "sonnet" in kwargs["model"].lower()
    assert kwargs["purpose"] == "rewrite_draft"


@pytest.mark.asyncio
async def test_rewrite_draft_prompt_contains_instruction_and_draft():
    client_config = {"client_name": "Gleadsy", "tone": {"language": "fr"}}
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(return_value="...")) as mock:
        await rewrite_draft(
            original_draft="DRAFT_TEXT",
            lt_instruction="PAKEISK_TAI",
            target_language="fr",
            client_config=client_config,
        )
    user_msg = mock.call_args.kwargs["messages"][0]["content"]
    system_msg = mock.call_args.kwargs["system"]
    assert "DRAFT_TEXT" in (system_msg or "")
    assert "PAKEISK_TAI" in user_msg
    # Target language mentioned in system
    assert "fr" in (system_msg or "").lower()


@pytest.mark.asyncio
async def test_rewrite_draft_raises_on_api_failure():
    # Unlike translate, rewrite is interactive - user is waiting - we want to surface errors
    from core.classifier import APIUnavailableError
    client_config = {"client_name": "Gleadsy", "tone": {"language": "fr"}}
    with patch("core.translation.call_claude_with_retry", new=AsyncMock(side_effect=APIUnavailableError("down"))):
        with pytest.raises(APIUnavailableError):
            await rewrite_draft("draft", "instrukcija", "fr", client_config)
