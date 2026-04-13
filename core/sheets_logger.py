import csv
import logging
from datetime import datetime, timezone
from pathlib import Path

import config

logger = logging.getLogger(__name__)

CSV_PATH = Path(__file__).parent.parent / "test_replies.csv"

HEADERS = [
    "Timestamp", "Campaign", "Client ID", "Lead email", "Company",
    "Original message", "Classification", "Confidence",
    "Generated reply", "Sending account", "Status",
]


def log_test_reply(
    campaign_name: str,
    client_id: str,
    lead_email: str,
    company: str,
    original_message: str,
    classification: str,
    confidence: float,
    generated_reply: str,
    sending_account: str,
    status: str = "test_mode",
) -> None:
    try:
        write_header = not CSV_PATH.exists() or CSV_PATH.stat().st_size == 0
        with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            if write_header:
                writer.writerow(HEADERS)
            writer.writerow([
                datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC"),
                campaign_name,
                client_id,
                lead_email,
                company,
                original_message,
                classification,
                f"{confidence:.0%}",
                generated_reply,
                sending_account,
                status,
            ])
        logger.info("TEST_MODE: logged reply for %s to CSV (status=%s)", lead_email, status)
    except Exception as e:
        logger.error("Failed to log to CSV: %s", e, exc_info=True)
