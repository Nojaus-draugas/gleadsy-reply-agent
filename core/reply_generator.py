import json
import logging
import anthropic
from core.classifier import get_anthropic_client, call_claude_with_retry, APIUnavailableError
from prompts.reply import build_reply_system_prompt, REPLY_USER_PROMPTS, FAQ_MATCH_PROMPT, TIME_PARSE_PROMPT, MEETING_CONFIRMATION_PROMPT
from prompts.templates import format_few_shots, format_anti_patterns, format_faq_list, format_slots_for_prompt

logger = logging.getLogger(__name__)


async def generate_reply(
    prospect_message: str,
    classification: str,
    client_config: dict,
    few_shots: list[dict],
    anti_patterns: list[dict],
    available_slots: list[dict] | None = None,
    matching_faq: str | None = None,
) -> str:
    """Generate reply. Raises APIUnavailableError if Claude API is down."""
    system_prompt = build_reply_system_prompt(
        client_config,
        format_anti_patterns(anti_patterns),
        format_few_shots(few_shots),
    )

    template = REPLY_USER_PROMPTS.get(classification)
    if not template:
        return ""

    user_prompt = template.format(
        reply_text=prospect_message,
        slots_section=format_slots_for_prompt(available_slots) if available_slots else "",
        matching_faq=matching_faq or "",
    )

    return await call_claude_with_retry(
        model="claude-sonnet-4-20250514",
        max_tokens=512,
        system=system_prompt,
        messages=[{"role": "user", "content": user_prompt}],
    )


async def match_faq(reply_text: str, faq_list: list[dict]) -> dict:
    prompt = FAQ_MATCH_PROMPT.format(
        reply_text=reply_text,
        faq_list=format_faq_list(faq_list),
    )
    try:
        raw = await call_claude_with_retry(
            model="claude-sonnet-4-20250514",
            max_tokens=512,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(raw)
    except (json.JSONDecodeError, KeyError, IndexError):
        return {"faq_index": None, "confidence": 0.0, "adapted_answer": "Puikus klausimas! Detaliau galėčiau papasakoti per trumpą pokalbį."}
    except APIUnavailableError as e:
        logger.error(f"FAQ match failed — API unavailable: {e}")
        return {"faq_index": None, "confidence": 0.0, "adapted_answer": ""}


async def parse_time_confirmation(reply_text: str, offered_slots_json: str) -> dict:
    prompt = TIME_PARSE_PROMPT.format(
        reply_text=reply_text,
        offered_slots_json=offered_slots_json,
    )
    try:
        raw = await call_claude_with_retry(
            model="claude-sonnet-4-20250514",
            max_tokens=128,
            messages=[{"role": "user", "content": prompt}],
        )
        return json.loads(raw)
    except (json.JSONDecodeError, KeyError, IndexError):
        return {"confirmed_slot_index": None, "confidence": 0.0}
    except APIUnavailableError as e:
        logger.error(f"Time parse failed — API unavailable: {e}")
        return {"confirmed_slot_index": None, "confidence": 0.0}


async def generate_meeting_confirmation(time_str: str, meet_link: str, duration: int, client_config: dict) -> str:
    """Generate meeting confirmation. Raises APIUnavailableError if Claude API is down."""
    prompt = MEETING_CONFIRMATION_PROMPT.format(
        time_str=time_str, meet_link=meet_link, duration=duration,
    )
    system = f"Tu esi {client_config['client_name']} atstovas. Kalba: {client_config['tone']['language']}. Kreipinys: {client_config['tone']['addressing']}. Stilius: {client_config['tone']['personality']}. Pasirašymas: {client_config['tone']['sign_off']}, {client_config['tone']['sender_name']}"
    return await call_claude_with_retry(
        model="claude-sonnet-4-20250514",
        max_tokens=256,
        system=system,
        messages=[{"role": "user", "content": prompt}],
    )
