import pytest
from unittest.mock import AsyncMock, patch
from core.classifier import classify_reply, ClassificationResult


@pytest.mark.asyncio
async def test_classify_returns_structured_result():
    mock_response = AsyncMock()
    mock_response.content = [AsyncMock(text='{"category": "INTERESTED", "confidence": 0.95, "reasoning": "Nori susitikti"}')]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("core.classifier.get_anthropic_client", return_value=mock_client):
        result = await classify_reply("Taip, galime pasikalbėti", "Test Campaign", 1)

    assert isinstance(result, ClassificationResult)
    assert result.category == "INTERESTED"
    assert result.confidence == 0.95
    assert result.reasoning == "Nori susitikti"


@pytest.mark.asyncio
async def test_classify_invalid_json_returns_uncertain():
    mock_response = AsyncMock()
    mock_response.content = [AsyncMock(text="This is not JSON")]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("core.classifier.get_anthropic_client", return_value=mock_client):
        result = await classify_reply("Kažkoks tekstas", "Test Campaign", 1)

    assert result.category == "UNCERTAIN"
    assert result.confidence == 0.0


@pytest.mark.asyncio
async def test_classify_invalid_category_returns_uncertain():
    mock_response = AsyncMock()
    mock_response.content = [AsyncMock(text='{"category": "INVALID", "confidence": 0.9, "reasoning": "test"}')]

    mock_client = AsyncMock()
    mock_client.messages.create = AsyncMock(return_value=mock_response)

    with patch("core.classifier.get_anthropic_client", return_value=mock_client):
        result = await classify_reply("Kažkas", "Test Campaign", 1)

    assert result.category == "UNCERTAIN"
