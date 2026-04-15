import httpx
import asyncio
import logging
import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.instantly.ai/api/v2"


def _headers():
    return {"Authorization": f"Bearer {config.INSTANTLY_API_KEY}"}


async def send_reply(email_account: str, reply_to_uuid: str, subject: str, body_text: str) -> dict:
    """Send reply via Instantly API V2.

    Retry policy is conservative to avoid double-sends:
    - 429 (rate limit): retry with exponential backoff (safe — request not accepted)
    - Network errors (httpx.RequestError): retry (request likely never reached server)
    - 5xx server errors: do NOT retry — server may have processed the send before
      failing the response, retrying would duplicate the email to the prospect
    - 4xx other than 429: do NOT retry — client error, retrying won't help
    """
    payload = {
        "eaccount": email_account,
        "reply_to_uuid": reply_to_uuid,
        "subject": subject,
        "body": {"text": body_text},
    }
    async with httpx.AsyncClient(timeout=30) as client:
        for attempt in range(3):
            try:
                response = await client.post(
                    f"{BASE_URL}/emails/reply",
                    json=payload,
                    headers=_headers(),
                )
            except httpx.RequestError as e:
                # Connection / timeout — request likely didn't reach server, safe to retry
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

            # Any other status (2xx success, 4xx, 5xx) — return or raise immediately.
            # 5xx is intentionally NOT retried: the send may have succeeded server-side.
            response.raise_for_status()
            return response.json()
    raise RuntimeError("send_reply: unexpected exit from retry loop")


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
