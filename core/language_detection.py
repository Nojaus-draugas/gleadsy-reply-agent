"""Offline language detection via langdetect with hint-based override logic.

Used in reply agent pipeline to determine target reply language when a prospect
responds. Per-campaign YAML config provides the primary hint; if detected
language differs strongly, the detection wins (user's actual language matters).
"""
import logging
from langdetect import detect, detect_langs, DetectorFactory, LangDetectException

# Determinism - required for consistent test results
DetectorFactory.seed = 0

logger = logging.getLogger(__name__)

SUPPORTED_LANGUAGES = {"lt", "en", "fr", "de", "et", "lv"}

# Text shorter than this is considered too unreliable to override hint
MIN_TEXT_FOR_DETECTION = 30

# Confidence below this means we trust hint over detection
DETECTION_CONFIDENCE_OVERRIDE_THRESHOLD = 0.90


def detect_language(text: str, campaign_hint: str) -> str:
    """Detect language of text, with campaign_hint as tie-breaker.

    Returns ISO-639-1 code from SUPPORTED_LANGUAGES, or campaign_hint if
    detection is unreliable. Falls back to "en" if detection returns an
    unsupported language and campaign_hint is also not supported.
    """
    hint = (campaign_hint or "en").lower()
    if hint not in SUPPORTED_LANGUAGES:
        hint = "en"

    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) < MIN_TEXT_FOR_DETECTION:
        return hint

    try:
        ranked = detect_langs(cleaned)
    except LangDetectException:
        logger.warning("langdetect failed on text: %s", cleaned[:80])
        return hint

    if not ranked:
        return hint

    top = ranked[0]
    detected_lang = top.lang.lower()
    detected_conf = top.prob

    # Detection matches hint - cheap confirm
    if detected_lang == hint:
        return hint

    # Detection is clearly different and confident - override
    if detected_conf >= DETECTION_CONFIDENCE_OVERRIDE_THRESHOLD:
        if detected_lang in SUPPORTED_LANGUAGES:
            logger.info(
                "Language override: campaign hint=%s detected=%s (conf=%.2f)",
                hint, detected_lang, detected_conf,
            )
            return detected_lang
        # Unsupported language detected with high confidence - fallback to "en"
        logger.warning(
            "Detected unsupported language %s (conf=%.2f), fallback to en",
            detected_lang, detected_conf,
        )
        return "en"

    # Detection uncertain - trust hint
    return hint
