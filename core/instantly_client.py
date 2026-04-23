import httpx
import asyncio
import logging
import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.instantly.ai/api/v2"


def _headers():
    return {"Authorization": f"Bearer {config.INSTANTLY_API_KEY}"}


async def send_reply(
    email_account: str,
    reply_to_uuid: str,
    subject: str,
    body_text: str,
    attachments: list[dict] | None = None,
) -> dict:
    """Send reply via Instantly API V2.

    Args:
        attachments: Optional list of dicts: [{"name": "file.pdf", "content": "<base64>", "type": "application/pdf"}]

    Retry policy is conservative to avoid double-sends:
    - 429 (rate limit): retry with exponential backoff (safe - request not accepted)
    - Network errors (httpx.RequestError): retry (request likely never reached server)
    - 5xx server errors: do NOT retry - server may have processed the send before
      failing the response, retrying would duplicate the email to the prospect
    - 4xx other than 429: do NOT retry - client error, retrying won't help
    """
    payload = {
        "eaccount": email_account,
        "reply_to_uuid": reply_to_uuid,
        "subject": subject,
        "body": {"text": body_text},
    }
    if attachments:
        payload["attachments"] = attachments
        logger.info(f"Sending reply with {len(attachments)} attachment(s): {[a.get('name') for a in attachments]}")
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(3):
            try:
                response = await client.post(
                    f"{BASE_URL}/emails/reply",
                    json=payload,
                    headers=_headers(),
                )
            except httpx.RequestError as e:
                # Connection / timeout - request likely didn't reach server, safe to retry
                if attempt == 2:
                    raise
                wait = 2 ** attempt
                logger.warning(f"Instantly network error {e!r}, retrying in {wait}s")
                await asyncio.sleep(wait)
                continue

            if response.status_code == 429:
                if attempt == 2:
                    raise httpx.HTTPStatusError(
                        "Rate limited after 3 attempts",
                        request=response.request, response=response,
                    )
                wait = 2 ** attempt
                logger.warning(f"Instantly rate limited, retrying in {wait}s")
                await asyncio.sleep(wait)
                continue

            # Any other status (2xx success, 4xx, 5xx) - return or raise immediately.
            # 5xx is intentionally NOT retried: the send may have succeeded server-side.
            response.raise_for_status()
            return response.json()
    raise RuntimeError("send_reply: unexpected exit from retry loop")


async def add_to_blocklist(email_or_domain: str) -> dict:
    """Pridėti email (arba @domain) į Instantly globalų blocklist.

    Po šito - net jei kitas lead bandys paleisti kampaniją šiuo adresu, Instantly blokuos.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        payload = {"bl_value": email_or_domain}
        if config.INSTANTLY_WORKSPACE_ID:
            payload["workspace_id"] = config.INSTANTLY_WORKSPACE_ID
        try:
            response = await client.post(
                f"{BASE_URL}/block-lists-entries",
                json=payload,
                headers=_headers(),
            )
            if response.status_code in (200, 201):
                logger.info(f"Blocklisted: {email_or_domain}")
                return {"ok": True, "data": response.json()}
            if response.status_code == 409:
                # Already in blocklist
                return {"ok": True, "already_existed": True}
            logger.warning(f"Blocklist failed {response.status_code}: {response.text[:200]}")
            return {"ok": False, "status": response.status_code, "error": response.text[:300]}
        except Exception as e:
            logger.error(f"Blocklist exception for {email_or_domain}: {e}")
            return {"ok": False, "error": str(e)}


async def delete_lead_by_email(lead_email: str, campaign_id: str = "") -> dict:
    """Ištrinti lead'ą iš kampanijos (arba visų, jei campaign_id tuščias).

    1. Randa lead'us per /leads/list su search filter.
    2. DELETE kiekvieną rastą.
    """
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            list_payload = {"search": lead_email, "limit": 10}
            if campaign_id:
                list_payload["campaign"] = campaign_id
            if config.INSTANTLY_WORKSPACE_ID:
                list_payload["workspace_id"] = config.INSTANTLY_WORKSPACE_ID
            response = await client.post(
                f"{BASE_URL}/leads/list",
                json=list_payload,
                headers=_headers(),
            )
            response.raise_for_status()
            data = response.json()
            items = data.get("items", data.get("data", []))
        except Exception as e:
            logger.error(f"Lead lookup failed for {lead_email}: {e}")
            return {"ok": False, "deleted": 0, "error": str(e)}

        if not items:
            return {"ok": True, "deleted": 0, "reason": "not found"}

        deleted = 0
        errors = []
        for lead in items:
            lead_id = lead.get("id")
            if not lead_id:
                continue
            # Tik jei email sutampa (search gali grąžinti partial matches)
            lead_email_found = (lead.get("email") or "").lower()
            if lead_email_found != lead_email.lower():
                continue
            try:
                del_response = await client.delete(
                    f"{BASE_URL}/leads/{lead_id}",
                    headers=_headers(),
                )
                if del_response.status_code in (200, 204):
                    deleted += 1
                else:
                    errors.append(f"{lead_id}: {del_response.status_code}")
            except Exception as e:
                errors.append(f"{lead_id}: {e}")

        if deleted:
            logger.info(f"Deleted {deleted} lead(s) matching {lead_email}")
        return {"ok": True, "deleted": deleted, "errors": errors}


async def poll_sent_emails(since_timestamp: str) -> list[dict]:
    """Poll Instantly for emails SENT by Paulius (human) since given timestamp.

    Used by auto-learn loop: agent captures Paulius's real replies and uses them
    as few-shot examples for future drafts. Skips auto-sent replies (agent's own).
    Returns list with email_id, lead_email, subject, body_text, timestamp.
    """
    all_sent = []
    starting_after = None

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                params = {
                    "workspace_id": config.INSTANTLY_WORKSPACE_ID,
                    "timestamp_created_after": since_timestamp,
                    "email_type": "sent",
                    "limit": 100,
                }
                if starting_after:
                    params["starting_after"] = starting_after
                response = await client.get(f"{BASE_URL}/emails", params=params, headers=_headers())
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPError as e:
                logger.error(f"Instantly sent-email polling error: {e}")
                break

            items = data.get("items", data.get("data", []))
            for email in items:
                body = email.get("body", {})
                # Instantly V2 grąžina body kaip {"html": "..."} - extract text
                import re
                if isinstance(body, dict):
                    body_text = body.get("text") or ""
                    if not body_text and body.get("html"):
                        body_text = re.sub(r"<br\s*/?>", "\n", body["html"], flags=re.IGNORECASE)
                        body_text = re.sub(r"</?(div|p|h\d)[^>]*>", "\n", body_text, flags=re.IGNORECASE)
                        body_text = re.sub(r"<[^>]+>", " ", body_text)
                        body_text = re.sub(r"[ \t]+", " ", body_text)
                        body_text = re.sub(r"\n\s*\n", "\n\n", body_text).strip()
                else:
                    body_text = str(body)
                all_sent.append({
                    "email_id": email.get("id", ""),
                    "reply_to_uuid": email.get("reply_to_uuid", ""),  # parent email
                    "lead_email": email.get("lead") or email.get("to_address_email_list", ""),
                    "from_account": email.get("eaccount", ""),
                    "campaign_id": email.get("campaign_id", ""),
                    "subject": email.get("subject", ""),
                    "body_text": body_text,
                    "timestamp": email.get("timestamp_created", ""),
                    # Instantly sequence step: "0_1_0" = cold sequence step (not a personal reply).
                    # Empty/null = manual reply by Paulius.
                    "step": email.get("step") or "",
                    "ue_type": email.get("ue_type"),
                })

            starting_after = data.get("next_starting_after")
            if not starting_after or not items:
                break

    if all_sent:
        logger.info(f"Polled {len(all_sent)} sent emails from Instantly")
    return all_sent


async def poll_for_replies(since_timestamp: str) -> list[dict]:
    """Poll Instantly for new replies since given timestamp. Returns webhook-compatible payloads.
    Handles pagination via next_starting_after cursor."""
    all_replies = []
    starting_after = None

    async with httpx.AsyncClient(timeout=30) as client:
        while True:
            try:
                params = {
                    "workspace_id": config.INSTANTLY_WORKSPACE_ID,
                    "timestamp_created_after": since_timestamp,
                    "email_type": "received",
                    "limit": 100,
                }
                if starting_after:
                    params["starting_after"] = starting_after

                response = await client.get(
                    f"{BASE_URL}/emails",
                    params=params,
                    headers=_headers(),
                )
                response.raise_for_status()
                data = response.json()
            except httpx.HTTPError as e:
                logger.error(f"Instantly polling error: {e}")
                break

            items = data.get("items", data.get("data", []))
            for email in items:
                body = email.get("body", {})
                reply_text = body.get("text", "") if isinstance(body, dict) else str(body)
                all_replies.append({
                    "event_type": "reply_received",
                    "campaign_id": email.get("campaign_id", ""),
                    "campaign_name": "",  # API v2 doesn't return campaign_name
                    "lead_email": email.get("from_address_email", email.get("lead", "")),
                    "email_account": email.get("eaccount", email.get("to_address_email_list", "")),
                    "email_id": email.get("id", ""),
                    "reply_text": reply_text,
                    "reply_subject": email.get("subject", ""),
                    "timestamp": email.get("timestamp_created", ""),
                })

            # Pagination: follow next_starting_after cursor
            starting_after = data.get("next_starting_after")
            if not starting_after or not items:
                break

    if all_replies:
        logger.info(f"Polled {len(all_replies)} new replies from Instantly")
    return all_replies
