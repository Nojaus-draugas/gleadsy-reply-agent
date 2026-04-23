import json
import re
import logging
import asyncio
import contextvars
import anthropic
from dataclasses import dataclass
from prompts.classify import CLASSIFY_SYSTEM_PROMPT, build_classify_user_prompt
import config

logger = logging.getLogger(__name__)

# Per-request usage akumuliatorius - resetinamas webhook'o pradžioje, kaupia visus
# Claude call'us per vieno lead'o atsakymo apdorojimą. Skaitomas log_interaction metu.
_usage_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("claude_usage", default=None)


def reset_usage_context() -> None:
    """Iškviečiama webhook'o pradžioje, prieš bet kokius Claude call'us."""
    _usage_ctx.set({"tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0, "tokens_cache_write": 0, "cost_usd": 0.0})


def get_usage_snapshot() -> dict:
    """Grąžina dabartinio request'o suminę statistiką (arba nulius jei nebuvo reset'inta)."""
    ctx = _usage_ctx.get()
    if ctx is None:
        return {"tokens_in": 0, "tokens_out": 0, "tokens_cache_read": 0, "cost_usd": 0.0}
    return {
        "tokens_in": ctx["tokens_in"],
        "tokens_out": ctx["tokens_out"],
        "tokens_cache_read": ctx["tokens_cache_read"],
        "cost_usd": round(ctx["cost_usd"], 6),
    }

VALID_CATEGORIES = {"ORDER_PLACED", "INTERESTED", "QUESTION", "NOT_NOW", "REFERRAL", "UNSUBSCRIBE", "OUT_OF_OFFICE", "UNCERTAIN"}

_client = None


def get_anthropic_client():
    global _client
    if _client is None:
        _client = anthropic.AsyncAnthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client


class APIUnavailableError(Exception):
    """Raised when Claude API is unreachable after retries (credits exhausted, outage, etc.)."""
    pass


def _log_usage(model: str, response, purpose: str) -> None:
    """Log token usage + cost as structured line. Safe - swallows errors.
    Taip pat akumuliuoja į per-request contextvar (reset_usage_context + get_usage_snapshot)."""
    try:
        usage = response.usage
        pricing = config.MODEL_PRICING.get(model, {})
        input_tok = getattr(usage, "input_tokens", 0) or 0
        output_tok = getattr(usage, "output_tokens", 0) or 0
        cache_read = getattr(usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(usage, "cache_creation_input_tokens", 0) or 0
        cost = (
            input_tok * pricing.get("input", 0)
            + output_tok * pricing.get("output", 0)
            + cache_read * pricing.get("cache_read", 0)
            + cache_write * pricing.get("cache_write", 0)
        ) / 1_000_000
        logger.info(
            f"claude_usage model={model} purpose={purpose} "
            f"in={input_tok} out={output_tok} cache_read={cache_read} cache_write={cache_write} "
            f"cost_usd={cost:.5f}"
        )
        ctx = _usage_ctx.get()
        if ctx is not None:
            ctx["tokens_in"] += input_tok
            ctx["tokens_out"] += output_tok
            ctx["tokens_cache_read"] += cache_read
            ctx["tokens_cache_write"] += cache_write
            ctx["cost_usd"] += cost
    except Exception as e:
        logger.debug(f"usage log failed: {e}")


async def call_claude_with_retry(*, model: str, max_tokens: int, system=None,
                                  messages: list, max_retries: int = 3,
                                  cache_system: bool = False, purpose: str = "unknown") -> str:
    """Call Claude API with exponential backoff retry. Raises APIUnavailableError on permanent failure.

    If cache_system=True and `system` is a string, wraps it as a cacheable block (ephemeral, 5min TTL).
    Pass `system` as a list of blocks to cache manually.
    """
    client = get_anthropic_client()

    if cache_system and isinstance(system, str) and system:
        system_param = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    else:
        system_param = system

    for attempt in range(max_retries):
        try:
            kwargs = {"model": model, "max_tokens": max_tokens, "messages": messages}
            if system_param:
                kwargs["system"] = system_param
            response = await client.messages.create(**kwargs)
            _log_usage(model, response, purpose)
            return response.content[0].text.strip()

        except anthropic.RateLimitError as e:
            wait = 2 ** attempt
            logger.warning(f"Claude rate limited (attempt {attempt + 1}/{max_retries}), retrying in {wait}s")
            if attempt == max_retries - 1:
                raise APIUnavailableError(f"Rate limited after {max_retries} attempts: {e}") from e
            await asyncio.sleep(wait)

        except anthropic.AuthenticationError as e:
            # Bad API key or credits exhausted - no point retrying
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
            model=config.CLASSIFY_MODEL,
            max_tokens=256,
            system=CLASSIFY_SYSTEM_PROMPT,
            messages=[{"role": "user", "content": user_prompt}],
            cache_system=True,
            purpose="classify",
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
