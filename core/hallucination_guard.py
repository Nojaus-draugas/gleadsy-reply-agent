"""Deterministic regex guard po reply generation'o.

Tikrina ar sugeneruotame atsakyme nėra konkrečių faktų (telefonų, email'ų,
eurų sumų, URL'ų), kurių NĖRA kliento brief'e arba pasiūlytuose slot'uose.

Jei randama - grąžina issue sąrašą, webhook'as eskaluoja į žmogų.
"""
import re
import logging

logger = logging.getLogger(__name__)

# Lietuviški + tarptautiniai telefonų formatai
PHONE_RE = re.compile(r"(?:\+370|8)\s?[\d\s\-]{7,}|\+\d{1,3}[\d\s\-]{7,}")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
URL_RE = re.compile(r"https?://[^\s\)]+|\bwww\.[^\s\)]+", re.IGNORECASE)
# Eurų sumos: 100€, 100 EUR, 100 eur, €100
MONEY_RE = re.compile(r"\d[\d\s.,]*\s?(?:€|EUR|eur(?![a-zA-ZąčęėįšųūžĄČĘĖĮŠŲŪŽ]))|€\s?\d[\d\s.,]*")


def _collect_allowed_from_brief(client_config: dict) -> dict[str, set[str]]:
    """Surenka visus leistinus faktus iš kliento konfig'o (normalizuotus)."""
    blob_parts = []
    for key in ("company_description", "service_offering", "value_proposition", "pricing"):
        val = client_config.get(key)
        if isinstance(val, str):
            blob_parts.append(val)
    # FAQ taip pat leistina - ten dažnai yra teisėtos kainos, kontaktai
    for faq in client_config.get("faq", []) or []:
        if isinstance(faq, dict):
            blob_parts.append(faq.get("answer", ""))
            blob_parts.append(faq.get("question", ""))
        elif isinstance(faq, str):
            blob_parts.append(faq)
    blob = " ".join(blob_parts)

    return {
        "phones": {_normalize_phone(p) for p in PHONE_RE.findall(blob)},
        "emails": {e.lower() for e in EMAIL_RE.findall(blob)},
        "urls": {_normalize_url(u) for u in URL_RE.findall(blob)},
        "money": {_normalize_money(m) for m in MONEY_RE.findall(blob)},
    }


def _normalize_phone(s: str) -> str:
    return re.sub(r"[\s\-]", "", s)


def _normalize_url(s: str) -> str:
    return s.lower().rstrip("/").replace("https://", "").replace("http://", "").replace("www.", "")


def _normalize_money(s: str) -> str:
    return re.sub(r"\s", "", s).lower()


def check_reply(reply_text: str, client_config: dict, offered_slots_text: str = "") -> list[str]:
    """Grąžina issue string'us jei radome halucinacijų. Tuščias sąrašas = švaru.

    Tikrinam 4 kategorijas: telefonai, email'ai, URL'ai, pinigų sumos.
    Leidžiame tik tai, kas yra brief'e arba pasiūlytuose slot'uose.
    """
    allowed = _collect_allowed_from_brief(client_config)
    issues: list[str] = []

    for phone in PHONE_RE.findall(reply_text):
        if _normalize_phone(phone) not in allowed["phones"]:
            issues.append(f"halucinacija: telefonas '{phone.strip()}' nėra brief'e")

    for email in EMAIL_RE.findall(reply_text):
        if email.lower() not in allowed["emails"]:
            issues.append(f"halucinacija: email '{email}' nėra brief'e")

    for url in URL_RE.findall(reply_text):
        if _normalize_url(url) not in allowed["urls"]:
            issues.append(f"halucinacija: URL '{url}' nėra brief'e")

    for money in MONEY_RE.findall(reply_text):
        if _normalize_money(money) not in allowed["money"]:
            issues.append(f"halucinacija: suma '{money.strip()}' nėra brief'e ar kainodaroje")

    if issues:
        logger.warning(f"hallucination_guard: {len(issues)} issue(s): {issues}")

    return issues
