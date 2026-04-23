import hashlib
import time
import httpx
import logging
import config

logger = logging.getLogger(__name__)

# In-memory dedup: suppress identical Slack messages sent within the last 24h.
# Second safety net in case webhook-level is_duplicate fails (e.g. after DB wipe
# on Render restart). Keyed by sha1(text); stores first-sent epoch.
_RECENT: dict[str, float] = {}
_DEDUP_TTL = 24 * 3600


def _dedup_hit(text: str) -> bool:
    now = time.time()
    # purge expired
    for k, ts in list(_RECENT.items()):
        if now - ts > _DEDUP_TTL:
            del _RECENT[k]
    key = hashlib.sha1(text.encode("utf-8", "ignore")).hexdigest()
    if key in _RECENT:
        return True
    _RECENT[key] = now
    return False


async def _send(text: str):
    if not config.SLACK_WEBHOOK_URL:
        logger.debug(f"Slack (no webhook configured): {text[:100]}")
        return
    if _dedup_hit(text):
        logger.info(f"Slack dedup: suppressing repeat within 24h: {text[:80]}")
        return
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            await client.post(config.SLACK_WEBHOOK_URL, json={"text": text})
    except httpx.HTTPError as e:
        logger.error(f"Slack notification failed: {e}")


async def notify_reply_sent(lead_email: str, campaign_name: str, category: str, confidence: float, prospect_text: str, agent_reply: str):
    await _send(
        f"📩 Reply išsiųstas | Kam: {lead_email} | Kampanija: {campaign_name} | "
        f"Klasifikacija: {category} ({confidence:.0%}) | "
        f"Prospektas: >{prospect_text[:150]} | Agentas: >{agent_reply[:150]}"
    )


async def notify_escalation(lead_email: str, campaign_name: str, reason: str, reply_text: str):
    await _send(
        f"⚠️ Reikia žmogaus | {lead_email} | {campaign_name} | "
        f"Priežastis: {reason} | Žinutė: >{reply_text[:200]}"
    )


async def notify_unknown_campaign(campaign_id: str, lead_email: str):
    await _send(
        f"❓ Nežinoma kampanija | campaign_id: {campaign_id} | lead: {lead_email} | "
        f"Reikia pridėti į kliento YAML"
    )


async def notify_meeting_booked(lead_email: str, campaign_name: str, time_str: str):
    await _send(f"📅 Susitikimas suplanuotas! | {lead_email} | {campaign_name} | Laikas: {time_str}")


async def notify_lead_document(
    lead_email: str,
    campaign_name: str,
    client_id: str,
    prospect_text: str,
    attachment_names: list[str],
    subject: str = "",
    dashboard_base_url: str = "",
):
    """Prospect'as atsiunte priedu (PDF/DOCX/Excel). Neatsakinejame automatiskai,
    Paulius turi peziureti per Instantly unibox rankomis.
    """
    names_str = ", ".join(attachment_names[:5])
    if len(attachment_names) > 5:
        names_str += f" (+{len(attachment_names) - 5} more)"
    link = f"\n👉 {dashboard_base_url.rstrip('/')}/replies" if dashboard_base_url else ""
    subj = f" | Subject: {subject[:60]}" if subject else ""
    await _send(
        f"📎 LEAD atsiunte dokumenta ({len(attachment_names)} vnt.) | {lead_email} | "
        f"{client_id} | {campaign_name}{subj}\n"
        f"Priedai: {names_str}\n"
        f"> {_preview(prospect_text, 300)}\n"
        f"⚠️ AUTO-REPLY PRALEISTAS - atidaryk Instantly unibox, atsisiusk priedus, "
        f"atsakyk rankomis (kontekstas yra dokumente, ne tekste){link}"
    )


async def notify_order_placed(
    lead_email: str,
    campaign_name: str,
    client_id: str,
    prospect_text: str,
    confidence: float,
    dashboard_base_url: str = "",
):
    """KRITINE notifikacija - prospect'as ka tik uzsake / patvirtino pirkima.
    Eina tiek slack, tiek email (zr. notify_order_placed_email)."""
    link = f"\n👉 {dashboard_base_url.rstrip('/')}/pending" if dashboard_base_url else ""
    await _send(
        f"🚨🛒 UZSAKYMAS PATVIRTINTAS | {lead_email} | {client_id} | Kampanija: {campaign_name} | "
        f"Conf: {confidence:.0%}\n"
        f"> {_preview(prospect_text, 400)}\n"
        f"⚠️ Draftas LAUKIA tavo approval - NIEKO automatiskai nesiuciama!{link}"
    )


async def notify_error(error_type: str, error_message: str):
    await _send(f"🔴 Klaida | {error_type} | {error_message[:200]}")


async def send_weekly_digest(stats: dict, week_start: str, week_end: str, confidence_old: float, confidence_new: float):
    categories = stats.get("categories", {})
    total = stats.get("total", 0)

    cat_lines = []
    for cat in ["INTERESTED", "QUESTION", "NOT_NOW", "REFERRAL", "UNSUBSCRIBE", "OUT_OF_OFFICE", "UNCERTAIN"]:
        n = categories.get(cat, 0)
        pct = (n / total * 100) if total > 0 else 0
        cat_lines.append(f"├ {cat}: {n} ({pct:.0f}%)")

    meetings = stats.get("meetings_count", 0)
    conv = (meetings / total * 100) if total > 0 else 0

    text = (
        f"📊 Savaitinė ataskaita ({week_start} - {week_end})\n\n"
        f"📩 Iš viso reply'ų: {total}\n"
        + "\n".join(cat_lines) + "\n\n"
        f"📅 Susitikimai suplanuoti: {meetings}\n"
        f"📈 Reply → Meeting: {conv:.0f}%\n\n"
        f"👍 {stats.get('thumbs_up', 0)} | 👎 {stats.get('thumbs_down', 0)} | Override: {stats.get('override_count', 0)}\n\n"
        f"🔧 Confidence threshold: {confidence_old} → {confidence_new}"
    )
    await _send(text)


LANG_FLAGS = {
    "lt": "🇱🇹", "en": "🇬🇧", "fr": "🇫🇷",
    "de": "🇩🇪", "et": "🇪🇪", "lv": "🇱🇻",
}


def _approval_prefix(classification: str, quality_score: int | None) -> str:
    if classification == "INTERESTED":
        return "🔥"
    if quality_score is not None and quality_score < 7:
        return "⚠️"
    return "⏳"


def _preview(text: str, max_chars: int = 240) -> str:
    text = (text or "").strip().replace("\n", " ")
    if len(text) > max_chars:
        return text[:max_chars].rstrip() + "..."
    return text


async def notify_approval_pending(
    iid: int,
    lead_email: str,
    client_id: str,
    classification: str,
    quality_score: int | None,
    confidence: float,
    prospect_message_lt: str,
    agent_reply_lt: str,
    original_language: str,
    dashboard_base_url: str,
) -> None:
    """Notify Paulius that a new foreign-language draft is waiting for approval."""
    prefix = _approval_prefix(classification, quality_score)
    flag = LANG_FLAGS.get(original_language.lower(), "")
    quality_str = f"Quality: {quality_score}/10" if quality_score is not None else "Quality: -"
    link = f"{dashboard_base_url.rstrip('/')}/pending#draft-{iid}"

    text = (
        f"{prefix} Naujas draftas laukia approval\n\n"
        f"Klientas: {client_id}  |  Lead: {lead_email}  {flag} {original_language.upper()}\n"
        f"Kategorija: {classification}  |  {quality_str}  |  Confidence: {confidence:.0%}\n\n"
        f"🗣️ Lead žinutė (LT vertimas):\n> {_preview(prospect_message_lt, 300)}\n\n"
        f"✍️ Agent'o draftas (LT preview):\n> {_preview(agent_reply_lt, 400)}\n\n"
        f"👉 {link}"
    )
    await _send(text)
