"""Translation helpers for foreign-language reply approval flow.

translate_to_lt: prospect_message / agent_reply -> LT preview (graceful on failure).
rewrite_draft: LT instruction + existing draft -> rewritten draft in target language.
"""
import logging
import config
from core.classifier import call_claude_with_retry, APIUnavailableError

logger = logging.getLogger(__name__)

LANGUAGE_NAMES = {
    "lt": "Lithuanian", "en": "English", "fr": "French",
    "de": "German", "et": "Estonian", "lv": "Latvian",
}


async def translate_to_lt(text: str, source_language: str) -> str:
    """Translate text to Lithuanian. No-op if source is 'lt' or text is empty.

    Returns "" on API failure (graceful degradation - dashboard will show
    only the original text, caller logs but does not raise).
    """
    if not text or not text.strip():
        return text
    if (source_language or "").lower() == "lt":
        return text

    source_name = LANGUAGE_NAMES.get(source_language.lower(), source_language)
    system = (
        f"You translate {source_name} text into Lithuanian. "
        "Translate faithfully, keeping tone and intent. "
        "Return ONLY the Lithuanian translation, no quotes, no explanations, no prefixes."
    )
    try:
        result = await call_claude_with_retry(
            model=config.TRANSLATION_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": text}],
            purpose="translate_to_lt",
        )
    except APIUnavailableError as e:
        logger.error("translate_to_lt failed (source=%s): %s", source_language, e)
        return ""
    return (result or "").strip()


async def rewrite_draft(
    original_draft: str,
    lt_instruction: str,
    target_language: str,
    client_config: dict,
) -> str:
    """Rewrite a draft reply based on Lithuanian instruction from user.

    The draft stays in target_language; the instruction is in LT. Returns the
    rewritten draft in target_language. Raises APIUnavailableError on failure
    (UI must surface this - user is waiting in edit modal).
    """
    target_name = LANGUAGE_NAMES.get(target_language.lower(), target_language)
    client_name = client_config.get("client_name", "")
    tone = client_config.get("tone", {})
    sign_off = tone.get("sign_off", "")
    sender = tone.get("sender_name", "")
    personality = tone.get("personality", "")

    system = (
        f"Tu esi {client_name} atstovas. Tu jau parašei šį draftą {target_name} kalba:\n\n"
        f"<draft>\n{original_draft}\n</draft>\n\n"
        f"Vartotojas nori perrašyti draftą pagal lietuviškas instrukcijas.\n"
        f"Perrašyk TOJE PAČIOJE {target_name} kalboje, išlaikydamas toną ir stilių.\n\n"
        f"Stilius: {personality}\n"
        f"Pasirašymas: {sign_off}, {sender}\n\n"
        f"KRITIŠKAI SVARBU:\n"
        f"- Grąžink TIK naują draftą, be jokių paaiškinimų, be kabučių, be prefiksų.\n"
        f"- Niekada nenaudok em-dash `—` ar en-dash `–`; rašyk trumpą brūkšnį `-`.\n"
        f"- Neįtrauk lietuviškų instrukcijų į atsakymą."
    )
    user_msg = f"Instrukcijos: {lt_instruction}"

    result = await call_claude_with_retry(
        model=config.REWRITE_MODEL,
        max_tokens=1024,
        system=system,
        messages=[{"role": "user", "content": user_msg}],
        purpose="rewrite_draft",
    )
    return (result or "").strip()
