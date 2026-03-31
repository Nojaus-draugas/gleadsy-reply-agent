import logging
from datetime import datetime, date, timedelta, time
from zoneinfo import ZoneInfo
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from google.auth.transport.requests import Request
from googleapiclient.discovery import build
import json
import config

logger = logging.getLogger(__name__)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
TZ = ZoneInfo(config.TIMEZONE)

LITHUANIAN_DAY_NAMES = {
    "Monday": "pirmadienį", "Tuesday": "antradienį", "Wednesday": "trečiadienį",
    "Thursday": "ketvirtadienį", "Friday": "penktadienį", "Saturday": "šeštadienį", "Sunday": "sekmadienį",
}
LITHUANIAN_MONTHS = {
    1: "sausio", 2: "vasario", 3: "kovo", 4: "balandžio", 5: "gegužės", 6: "birželio",
    7: "liepos", 8: "rugpjūčio", 9: "rugsėjo", 10: "spalio", 11: "lapkričio", 12: "gruodžio",
}


def _get_calendar_service():
    creds = None
    token_path = config.GOOGLE_CALENDAR_TOKEN_PATH
    creds_path = config.GOOGLE_CALENDAR_CREDENTIALS_PATH

    try:
        creds = Credentials.from_authorized_user_file(token_path, SCOPES)
    except (FileNotFoundError, ValueError):
        pass

    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        except Exception as e:
            logger.error(f"Google Calendar token refresh failed: {e}")
            return None

    if not creds or not creds.valid:
        try:
            flow = InstalledAppFlow.from_client_secrets_file(creds_path, SCOPES)
            creds = flow.run_local_server(port=0)
            with open(token_path, "w") as f:
                f.write(creds.to_json())
        except Exception as e:
            logger.error(f"Google Calendar auth failed: {e}")
            return None

    return build("calendar", "v3", credentials=creds)


def filter_working_hours_slots(
    busy_periods: list[dict],
    start_date: date,
    days_ahead: int,
    working_hours: dict,
    duration_minutes: int,
    buffer_minutes: int,
) -> list[dict]:
    """Generate available slots within working hours, excluding busy periods."""
    wh_start = time.fromisoformat(working_hours["start"])
    wh_end = time.fromisoformat(working_hours["end"])
    allowed_days = set(working_hours["days"])
    slot_duration = timedelta(minutes=duration_minutes)
    buffer = timedelta(minutes=buffer_minutes)

    # Parse busy periods into datetime pairs
    busy = []
    for bp in busy_periods:
        bs = datetime.fromisoformat(bp["start"])
        be = datetime.fromisoformat(bp["end"])
        busy.append((bs, be))

    slots = []
    for day_offset in range(days_ahead):
        current_date = start_date + timedelta(days=day_offset)
        day_name = current_date.strftime("%A")
        if day_name not in allowed_days:
            continue

        current_time = datetime.combine(current_date, wh_start, tzinfo=TZ)
        end_of_day = datetime.combine(current_date, wh_end, tzinfo=TZ)

        while current_time + slot_duration <= end_of_day:
            slot_end = current_time + slot_duration
            is_busy = any(bs < slot_end and be > current_time for bs, be in busy)

            if not is_busy:
                lt_day = LITHUANIAN_DAY_NAMES.get(day_name, day_name)
                slots.append({
                    "date": current_date.isoformat(),
                    "day_name": lt_day,
                    "time": current_time.strftime("%H:%M"),
                    "end": slot_end.strftime("%H:%M"),
                    "iso": current_time.isoformat(),
                })

            current_time += slot_duration + buffer

    return slots


async def get_free_slots(
    calendar_id: str,
    working_hours: dict,
    duration: int,
    advance_days: int,
    num_slots: int,
    buffer_minutes: int = 15,
) -> list[dict]:
    """Get free calendar slots for meeting scheduling."""
    service = _get_calendar_service()
    if not service:
        logger.error("Google Calendar service unavailable")
        return []

    now = datetime.now(TZ)
    # Start from tomorrow
    start_date = (now + timedelta(days=1)).date()
    time_min = datetime.combine(start_date, time.min, tzinfo=TZ).isoformat()
    time_max = datetime.combine(start_date + timedelta(days=advance_days), time.max, tzinfo=TZ).isoformat()

    try:
        body = {
            "timeMin": time_min,
            "timeMax": time_max,
            "timeZone": config.TIMEZONE,
            "items": [{"id": calendar_id}],
        }
        result = service.freebusy().query(body=body).execute()
        busy = result["calendars"][calendar_id]["busy"]
    except Exception as e:
        logger.error(f"Google Calendar FreeBusy error: {e}")
        return []

    all_slots = filter_working_hours_slots(
        busy_periods=busy,
        start_date=start_date,
        days_ahead=advance_days,
        working_hours=working_hours,
        duration_minutes=duration,
        buffer_minutes=buffer_minutes,
    )

    return all_slots[:num_slots]


async def create_meeting_event(
    calendar_id: str,
    prospect_email: str,
    start_iso: str,
    duration_minutes: int,
    meeting_purpose: str,
    client_participant: str,
) -> dict | None:
    """Create a Google Calendar event with Google Meet."""
    service = _get_calendar_service()
    if not service:
        return None

    start_dt = datetime.fromisoformat(start_iso)
    end_dt = start_dt + timedelta(minutes=duration_minutes)

    event = {
        "summary": meeting_purpose,
        "description": f"Susitikimas su {prospect_email}\nDalyvis: {client_participant}",
        "start": {"dateTime": start_dt.isoformat(), "timeZone": config.TIMEZONE},
        "end": {"dateTime": end_dt.isoformat(), "timeZone": config.TIMEZONE},
        "attendees": [{"email": prospect_email}],
        "conferenceData": {
            "createRequest": {"requestId": f"meet-{start_dt.timestamp()}", "conferenceSolutionKey": {"type": "hangoutsMeet"}},
        },
        "reminders": {"useDefault": False, "overrides": [{"method": "popup", "minutes": 15}]},
    }

    try:
        result = service.events().insert(
            calendarId=calendar_id,
            body=event,
            conferenceDataVersion=1,
        ).execute()
        return {
            "event_id": result["id"],
            "meet_link": result.get("hangoutLink", ""),
            "html_link": result.get("htmlLink", ""),
        }
    except Exception as e:
        logger.error(f"Google Calendar event creation error: {e}")
        return None


def format_slots_for_reply(slots: list[dict]) -> str:
    """Format slots in Lithuanian for natural text."""
    if not slots:
        return ""
    parts = []
    for s in slots:
        d = date.fromisoformat(s["date"])
        month = LITHUANIAN_MONTHS.get(d.month, "")
        parts.append(f"{s['day_name']} ({month} {d.day} d.) {s['time']}")
    if len(parts) == 1:
        return parts[0]
    return ", ".join(parts[:-1]) + " arba " + parts[-1]
