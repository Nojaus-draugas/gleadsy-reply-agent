import pytest
from unittest.mock import AsyncMock, patch
from core.slack_notifier import notify_approval_pending


@pytest.mark.asyncio
async def test_notify_approval_pending_sends_message():
    with patch("core.slack_notifier._send", new=AsyncMock()) as mock_send:
        await notify_approval_pending(
            iid=42,
            lead_email="pierre@acme.fr",
            client_id="gleadsy",
            classification="INTERESTED",
            quality_score=8,
            confidence=0.92,
            prospect_message_lt="Labas, įdomu. Kiek kainuoja?",
            agent_reply_lt="Ačiū už susidomėjimą. Kainos nuo 800€/mėn...",
            original_language="fr",
            dashboard_base_url="https://reply.gleadsy.com",
        )
    mock_send.assert_called_once()
    text = mock_send.call_args.args[0]
    assert "pierre@acme.fr" in text
    assert "gleadsy" in text
    assert "INTERESTED" in text
    assert "Labas, įdomu" in text
    assert "Ačiū už" in text
    assert "reply.gleadsy.com/pending#draft-42" in text
    assert "🔥" in text  # INTERESTED high-priority emoji


@pytest.mark.asyncio
async def test_notify_approval_pending_normal_priority_for_question():
    with patch("core.slack_notifier._send", new=AsyncMock()) as mock_send:
        await notify_approval_pending(
            iid=1, lead_email="x@y.com", client_id="c",
            classification="QUESTION", quality_score=9, confidence=0.8,
            prospect_message_lt="prompt", agent_reply_lt="reply",
            original_language="de",
            dashboard_base_url="https://reply.gleadsy.com",
        )
    text = mock_send.call_args.args[0]
    assert "⏳" in text or "🔥" not in text  # normal prefix


@pytest.mark.asyncio
async def test_notify_approval_pending_warning_for_low_quality():
    with patch("core.slack_notifier._send", new=AsyncMock()) as mock_send:
        await notify_approval_pending(
            iid=1, lead_email="x@y.com", client_id="c",
            classification="QUESTION", quality_score=6, confidence=0.8,
            prospect_message_lt="prompt", agent_reply_lt="reply",
            original_language="fr",
            dashboard_base_url="https://reply.gleadsy.com",
        )
    text = mock_send.call_args.args[0]
    assert "⚠️" in text


@pytest.mark.asyncio
async def test_notify_approval_pending_truncates_long_text():
    long_lt = "Sakinys. " * 100  # way over any reasonable preview
    with patch("core.slack_notifier._send", new=AsyncMock()) as mock_send:
        await notify_approval_pending(
            iid=1, lead_email="x@y.com", client_id="c",
            classification="QUESTION", quality_score=8, confidence=0.8,
            prospect_message_lt=long_lt, agent_reply_lt=long_lt,
            original_language="fr",
            dashboard_base_url="https://reply.gleadsy.com",
        )
    text = mock_send.call_args.args[0]
    # Each preview capped - total message reasonable
    assert len(text) < 2000
