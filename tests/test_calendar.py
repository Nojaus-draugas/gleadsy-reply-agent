import pytest
import os
os.environ.setdefault("TIMEZONE", "Europe/Vilnius")
from datetime import datetime, date
from core.calendar_manager import filter_working_hours_slots, format_slots_for_reply


def test_filter_working_hours_excludes_weekends():
    # Saturday
    slots = filter_working_hours_slots(
        busy_periods=[],
        start_date=date(2026, 4, 4),  # Saturday
        days_ahead=1,
        working_hours={"start": "09:00", "end": "17:00", "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]},
        duration_minutes=30,
        buffer_minutes=15,
    )
    assert len(slots) == 0


def test_filter_working_hours_respects_busy():
    slots = filter_working_hours_slots(
        busy_periods=[
            {"start": "2026-04-01T09:00:00+03:00", "end": "2026-04-01T12:00:00+03:00"},
        ],
        start_date=date(2026, 4, 1),  # Wednesday
        days_ahead=1,
        working_hours={"start": "09:00", "end": "17:00", "days": ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday"]},
        duration_minutes=30,
        buffer_minutes=15,
    )
    # All morning slots should be excluded
    for slot in slots:
        hour = int(slot["time"].split(":")[0])
        assert hour >= 12


def test_format_slots_for_reply():
    slots = [
        {"date": "2026-04-01", "day_name": "trečiadienį", "time": "10:00", "end": "10:30"},
        {"date": "2026-04-02", "day_name": "ketvirtadienį", "time": "14:00", "end": "14:30"},
    ]
    result = format_slots_for_reply(slots)
    assert "trečiadienį" in result
    assert "ketvirtadienį" in result
    assert "balandžio" in result
