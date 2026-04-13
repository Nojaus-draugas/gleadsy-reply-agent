import json
import re
import logging
import asyncio
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


class APIUnavailableError(Exception):
    """Raised when Claude API is unreachable after retries (credits exhausted, outage, etc.)."""
    pass


async def call_claude_with_retry(*, model: str, max_tokens: int, system: str | None = None,
                                  messages: list, max_retries: int = 3) -> str:
    """Call Claude API with exponential backoff retry. Raises APIUnavailableError on permanent failure."""
    client = get_anthropic_client()

    for attempt in range(max_retries):
        try:
            kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
            if system:
                kwargs["system"] = system
            response = await client.messages.create(**kwargs)
            return response.content[0].text.strip()

        except anthropic.RateLimitError as e:
            wait = 2 ** attempt
            logger.warning(f"Claude rate limited (attempt {attempt + 1}/{max_retries}), retrying in {wait}s")
            if attempt == max_retries - 1:
                raise APIUnavailableError(f"Rate limited after {max_retries} attempts: {e}") from e
            await asyncio.sleep(wait)

        except anthropic.AuthenticationError as e:
            # Bad API key or credits exhausted — no point retrying
            raise APIUnavailableError(f"Authentication failed (check API key/credits): {e}") from e

        except anthropic.APIStatusError as e:
            if e.status_code >= 500:
                wait = 2 ** attempt
                logger.warning(f"Claude server error {e.status_code} (attempt {attempt + 1}/{max_retries}), retrying in {wait}s")
                if attempt == max_retries - 1:
                    raise APIUnavailableError(f"Server error after {max_retries} attempts: {e}") from e
                await asyncio.sleep(wait)
            else:
                raise APIUnavailableError(f"Claude API error {e.status_code}: {e}") from e

        except anthropic.APIConnectionError as e:
            wait = 2 ** attempt
            logger.warning(f"Claude connection error (attempt {attempt + 1}/{max_retries}), retrying in {wait}s")
            if attempt == max_retries - 1:
                raise APIUnavailableError(f"Connection failed after {max_retries} attempts: {e}") from e
            await asyncio.sleep(wait)

    raise APIUnavailableError("Unexpected exit from retry loop")


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
    user_prompt = build_classify_user_prompt(reply_text, campaign_name, thread_position)

    try:
        raw = await call_claude_with_retry(
            model="claude-sonnet-4-20250514",
            max_tokens=256,
            system=CLASSIFY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
        )
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
        logger.warning(f"Classification parse failed for '{reply_text[:80]}...': error={e}")
        return ClassificationResult(category="UNCERTAIN", confidence=0.0, reasoning=f"Parse failed: {e}")
    except APIUnavailableError as e:
        logger.error(f"Claude API unavailable during classification: {e}")
        raise  # Let webhook handler deal with this
