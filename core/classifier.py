import json
import re
import logging
import anthropic
from dataclasses import dataclass
from prompts.classify import CLASSIFY_SYSTEM_PROMPT, build_classify_user_prompt
import config

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {"INTERESTED", "QUESTION", "NOT_NOW", "REFERRAL", "UNSUBSCRIBE", "OUT_OF_OFFICE", "UNCERTAIN"}

_client = None


def get_anthropic_client():
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


@dataclass
class ClassificationResult:
    category: str
    confidence: float
    reasoning: str


def _extract_json(raw: str) -> dict:
    """Try to extract JSON from response, handling markdown code blocks and extra text."""
    # Try direct parse first
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    # Try extracting from markdown code block
    match = re.search(r'```(?:json)?\s*(\{.*?\})\s*```', raw, re.DOTALL)
    if match:
        return json.loads(match.group(1))
    # Try finding first JSON object
    match = re.search(r'\{[^{}]*"category"[^{}]*\}', raw)
    if match:
        return json.loads(match.group(0))
    raise json.JSONDecodeError("No JSON found", raw, 0)


async def classify_reply(reply_text: str, campaign_name: str, thread_position: int) -> ClassificationResult:
    client = get_anthropic_client()
    user_prompt = build_classify_user_prompt(reply_text, campaign_name, thread_position)

    try:
        response = await client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=256,
            system=CLASSIFY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
        raw = response.content[0].text.strip()
        data = _extract_json(raw)

        category = data.get("category", "UNCERTAIN")
        if category not in VALID_CATEGORIES:
            category = "UNCERTAIN"

        return ClassificationResult(
            category=category,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=data.get("reasoning", ""),
        )
    except (json.JSONDecodeError, KeyError, IndexError) as e:
        logger.warning(f"Classification parse failed for '{reply_text[:80]}...': raw='{raw[:200]}' error={e}")
        return ClassificationResult(category="UNCERTAIN", confidence=0.0, reasoning=f"Parse failed: {raw[:100]}")
    except anthropic.APIError as e:
        return ClassificationResult(category="UNCERTAIN", confidence=0.0, reasoning=f"API error: {e}")
