import httpx
import asyncio
import logging
import config

logger = logging.getLogger(__name__)

BASE_URL = "https://api.instantly.ai/api/v2"


def _headers():
    return {"Authorization": f"Bearer {config.INSTANTLY_API_KEY}"}


async def send_reply(email_account: str, reply_to_uuid: str, subject: str, body_text: str) -> dict:
    """Send reply via Instantly API V2. Raises on failure after 3 retries."""
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
                response.raise_for_status()
                return response.json()
            except httpx.HTTPStatusError:
                if attempt == 2:
                    raise
                logger.warning(f"Instantly API error (attempt {attempt+1})")
                await asyncio.sleep(2 ** attempt)
    raise RuntimeError("send_reply: unexpected exit from retry loop")


async def poll_for_replies(since_timestamp: str) -> list[dict]:
    """Poll Instantly for new replies since given timestamp. Returns webhook-compatible payloads."""
    async with httpx.AsyncClient(timeout=30) as client:
        try:
            response = await client.get(
                f"{BASE_URL}/emails",
                params={
                    "workspace_id": config.INSTANTLY_WORKSPACE_ID,
                    "timestamp_created_after": since_timestamp,
                    "email_type": "received",
                },
                headers=_headers(),
            )
            response.raise_for_status()
            data = response.json()
        except httpx.HTTPError as e:
            logger.error(f"Instantly polling error: {e}")
            return []

    replies = []
    for email in data.get("data", []):
        replies.append({
            "event_type": "reply_received",
            "campaign_id": email.get("campaign_id", ""),
            "campaign_name": email.get("campaign_name", ""),
            "lead_email": email.get("from_address", ""),
            "email_account": email.get("to_address", ""),
            "email_id": email.get("id", ""),
            "reply_text": email.get("body", {}).get("text", ""),
            "reply_subject": email.get("subject", ""),
            "timestamp": email.get("timestamp_created", ""),
        })
    return replies
