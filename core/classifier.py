import json
import anthropic
from dataclasses import dataclass
from prompts.classify import CLASSIFY_SYSTEM_PROMPT, build_classify_user_prompt
import config

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
        data = json.loads(raw)

        category = data.get("category", "UNCERTAIN")
        if category not in VALID_CATEGORIES:
            category = "UNCERTAIN"

        return ClassificationResult(
            category=category,
            confidence=float(data.get("confidence", 0.0)),
            reasoning=data.get("reasoning", ""),
        )
    except (json.JSONDecodeError, KeyError, IndexError):
        return ClassificationResult(category="UNCERTAIN", confidence=0.0, reasoning="Failed to parse classification response")
    except anthropic.APIError as e:
        return ClassificationResult(category="UNCERTAIN", confidence=0.0, reasoning=f"API error: {e}")
