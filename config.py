import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# Paths
BASE_DIR = Path(__file__).parent
CLIENTS_DIR = BASE_DIR / "clients"
DB_PATH = BASE_DIR / "data" / "agent.db"

# Instantly
INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY", "")
INSTANTLY_WORKSPACE_ID = os.getenv("INSTANTLY_WORKSPACE_ID", "")
REPLY_SOURCE = os.getenv("REPLY_SOURCE", "webhook")  # "webhook" or "polling"
POLLING_INTERVAL_SECONDS = int(os.getenv("POLLING_INTERVAL_SECONDS", "60"))

# Claude API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Google Calendar
GOOGLE_CALENDAR_CREDENTIALS_PATH = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH", "./credentials.json")
GOOGLE_CALENDAR_TOKEN_PATH = os.getenv("GOOGLE_CALENDAR_TOKEN_PATH", "./token.json")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# Slack
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# Agent config
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.4"))
MAX_REPLIES_PER_THREAD = int(os.getenv("MAX_REPLIES_PER_THREAD", "5"))
REPLY_COOLDOWN_HOURS = int(os.getenv("REPLY_COOLDOWN_HOURS", "4"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Vilnius")

# Email notifications
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# Test mode — logs replies to Google Sheets instead of sending via Instantly
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"
