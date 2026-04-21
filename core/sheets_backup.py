"""Google Sheets backup for interactions - survives ephemeral Render disk wipes.

Strategy: every log_interaction() also appends a row to a Google Sheet.
On fresh DB startup, restore_from_sheet() pulls all rows back so classifications
don't need to be re-run via the Anthropic API.
"""
import logging
import os
import json
from datetime import datetime, timezone
from typing import Any, Optional

logger = logging.getLogger(__name__)

SHEET_ID = os.getenv("BACKUP_SHEET_ID", "")
SA_KEY_PATH = os.getenv("GOOGLE_SA_KEY_PATH", "secrets/sa-key.json")
SA_KEY_JSON = os.getenv("GOOGLE_SA_KEY_JSON", "")  # alternative: inline JSON via env
SHEET_NAME = "interactions"

# Must match db.log_interaction columns (in order)
COLUMNS = [
    "id", "created_at", "campaign_id", "campaign_name", "lead_email",
    "email_account", "email_id", "client_id", "prospect_message",
    "classification", "confidence", "classification_reasoning", "agent_reply",
    "was_sent", "matched_faq_index", "faq_confidence", "offered_slots",
    "few_shots_used", "thread_position", "brief_version",
    "quality_score", "quality_issues", "quality_summary",
]

_service = None


def _get_service():
    global _service
    if _service is not None:
        return _service
    if not SHEET_ID:
        return None
    try:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        scopes = ["https://www.googleapis.com/auth/spreadsheets"]
        if SA_KEY_JSON:
            info = json.loads(SA_KEY_JSON)
            creds = service_account.Credentials.from_service_account_info(info, scopes=scopes)
        elif os.path.exists(SA_KEY_PATH):
            creds = service_account.Credentials.from_service_account_file(SA_KEY_PATH, scopes=scopes)
        else:
            logger.warning("sheets_backup: no SA credentials found; backup disabled")
            return None
        _service = build("sheets", "v4", credentials=creds, cache_discovery=False)
        return _service
    except Exception as e:
        logger.error("sheets_backup: failed to init service: %s", e)
        return None


def _ensure_header() -> None:
    svc = _get_service()
    if not svc:
        return
    try:
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!A1:Z1"
        ).execute()
        values = result.get("values", [])
        if not values:
            svc.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"{SHEET_NAME}!A1",
                valueInputOption="RAW",
                body={"values": [COLUMNS]},
            ).execute()
    except Exception as e:
        # Sheet tab may not exist - try to create it
        try:
            svc.spreadsheets().batchUpdate(
                spreadsheetId=SHEET_ID,
                body={"requests": [{"addSheet": {"properties": {"title": SHEET_NAME}}}]},
            ).execute()
            svc.spreadsheets().values().update(
                spreadsheetId=SHEET_ID,
                range=f"{SHEET_NAME}!A1",
                valueInputOption="RAW",
                body={"values": [COLUMNS]},
            ).execute()
        except Exception as e2:
            logger.error("sheets_backup: ensure_header failed: %s / %s", e, e2)


def append_interaction(row: dict[str, Any]) -> None:
    """Append a single interaction row. Safe to call - silent if disabled."""
    svc = _get_service()
    if not svc:
        return
    try:
        _ensure_header()
        values = [[str(row.get(c, "") if row.get(c) is not None else "") for c in COLUMNS]]
        svc.spreadsheets().values().append(
            spreadsheetId=SHEET_ID,
            range=f"{SHEET_NAME}!A1",
            valueInputOption="RAW",
            insertDataOption="INSERT_ROWS",
            body={"values": values},
        ).execute()
    except Exception as e:
        logger.error("sheets_backup: append failed for %s: %s", row.get("lead_email"), e)


def fetch_all_rows() -> list[dict[str, Any]]:
    """Pull all backed-up interaction rows from the sheet."""
    svc = _get_service()
    if not svc:
        return []
    try:
        result = svc.spreadsheets().values().get(
            spreadsheetId=SHEET_ID, range=f"{SHEET_NAME}!A:Z"
        ).execute()
        values = result.get("values", [])
        if len(values) < 2:
            return []
        header = values[0]
        rows = []
        for r in values[1:]:
            d = {header[i]: (r[i] if i < len(r) else "") for i in range(len(header))}
            rows.append(d)
        return rows
    except Exception as e:
        logger.error("sheets_backup: fetch_all failed: %s", e)
        return []
