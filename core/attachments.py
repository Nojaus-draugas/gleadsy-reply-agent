"""Automated attachment selection + base64 encoding for Instantly API.

Agent reply text'e atpažįsta trigger frazes (per kalba), ir jei match -
parenka atitinkamą PDF failą is client_config['attachments'] + base64 encode'ina.

Native attachment per Instantly API v2 (be gleadsy.com URL'o).
"""
import base64
import logging
import re
from pathlib import Path

logger = logging.getLogger(__name__)

ATTACHMENTS_DIR = Path("/app/attachments")


def detect_attachments(
    client_config: dict,
    agent_reply: str,
    prospect_language: str = "lt",
) -> list[dict]:
    """Grąžina list'ą priedų kuriuos reikia prisegti pagal agent reply turinį.

    Kiekvienas elementas: {"name": "...", "content": "<base64>", "type": "application/pdf"}

    Args:
        client_config: kliento YAML kaip dict (turi 'attachments' dict'ą)
        agent_reply: Sugeneruotas agent atsakymas
        prospect_language: Aptikta prospect'o kalba (lt/en/fr) - parenka atitinkamą PDF
    """
    attachments_cfg = client_config.get("attachments") or {}
    if not attachments_cfg:
        return []

    reply_lower = agent_reply.lower()
    result = []

    for attachment_key, cfg in attachments_cfg.items():
        if not isinstance(cfg, dict):
            continue

        # 1) Patikrinti ar reply text contains trigger phrase (pagal kalbą)
        triggers = cfg.get("trigger_phrases", {})
        lang_triggers = triggers.get(prospect_language) or triggers.get("lt") or []
        matched = False
        for phrase in lang_triggers:
            if phrase.lower() in reply_lower:
                matched = True
                logger.info(f"Attachment trigger matched: '{phrase}' (lang={prospect_language}, key={attachment_key})")
                break

        if not matched:
            continue

        # 2) Parinkti tinkamą PDF failą pagal kalbą
        files = cfg.get("files", {})
        filename = files.get(prospect_language) or files.get("lt")
        if not filename:
            logger.warning(f"No file defined for attachment '{attachment_key}' language '{prospect_language}'")
            continue

        file_path = ATTACHMENTS_DIR / filename
        if not file_path.exists():
            logger.error(f"Attachment file not found: {file_path}")
            continue

        # 3) Base64 encode
        try:
            with open(file_path, "rb") as f:
                content_b64 = base64.b64encode(f.read()).decode("ascii")
        except Exception as e:
            logger.error(f"Failed to read attachment {file_path}: {e}")
            continue

        result.append({
            "name": filename,
            "content": content_b64,
            "type": cfg.get("mime_type", "application/pdf"),
        })
        logger.info(f"Prepared attachment: {filename} ({len(content_b64)} base64 chars)")

    return result


def detect_language_from_text(text: str) -> str:
    """Paprasta heuristic kalbos aptikimui jei nėra core/language_detection.
    Grąžina 'lt' / 'en' / 'fr'."""
    if not text:
        return "lt"
    t = text.lower()
    # FR markers
    fr_words = ["bonjour", "cordialement", "merci", "je vous", "nous avons", "ça", "être", "très", "poutres"]
    fr_count = sum(1 for w in fr_words if w in t)
    # EN markers
    en_words = ["hi ", "hello", "thanks", "thank you", "pricing", "could you", "please", "regards"]
    en_count = sum(1 for w in en_words if w in t)
    # LT markers (charų charakteristika)
    lt_chars = sum(1 for c in t if c in "ąčęėįšųūž")

    if fr_count >= 2:
        return "fr"
    if en_count >= 2 and lt_chars == 0:
        return "en"
    return "lt"
