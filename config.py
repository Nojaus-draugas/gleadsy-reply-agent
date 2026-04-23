import os
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(override=True)

# Paths
BASE_DIR = Path(__file__).parent
CLIENTS_DIR = BASE_DIR / "clients"
DB_PATH = Path(os.getenv("DB_PATH", str(BASE_DIR / "data" / "agent.db")))

# Instantly
INSTANTLY_API_KEY = os.getenv("INSTANTLY_API_KEY", "")
INSTANTLY_WORKSPACE_ID = os.getenv("INSTANTLY_WORKSPACE_ID", "")

# Claude API
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# Models - centralizuoti, lengvai keičiami per ENV
# Haiku 4.5 - pigus ir greitas klasifikacijai / FAQ match / time parse
# Sonnet 4.6 - reply generation ir quality review (svarbiau kokybė)
CLASSIFY_MODEL = os.getenv("CLASSIFY_MODEL", "claude-haiku-4-5-20251001")
REPLY_MODEL = os.getenv("REPLY_MODEL", "claude-sonnet-4-6")
QUALITY_MODEL = os.getenv("QUALITY_MODEL", "claude-sonnet-4-6")
FAQ_MATCH_MODEL = os.getenv("FAQ_MATCH_MODEL", "claude-haiku-4-5-20251001")
TIME_PARSE_MODEL = os.getenv("TIME_PARSE_MODEL", "claude-haiku-4-5-20251001")
MEETING_CONFIRM_MODEL = os.getenv("MEETING_CONFIRM_MODEL", "claude-haiku-4-5-20251001")
TRANSLATION_MODEL = os.getenv("TRANSLATION_MODEL", "claude-haiku-4-5-20251001")
REWRITE_MODEL = os.getenv("REWRITE_MODEL", "claude-sonnet-4-6")

# Cost per 1M tokens ($) - naudojama cost tracking'ui log'uose
# Šaltinis: anthropic.com/pricing (2026-04)
MODEL_PRICING = {
    "claude-haiku-4-5-20251001": {"input": 1.00, "output": 5.00, "cache_read": 0.10, "cache_write": 1.25},
    "claude-sonnet-4-6":         {"input": 3.00, "output": 15.00, "cache_read": 0.30, "cache_write": 3.75},
    "claude-opus-4-7":           {"input": 15.00, "output": 75.00, "cache_read": 1.50, "cache_write": 18.75},
}

# Google Calendar
GOOGLE_CALENDAR_CREDENTIALS_PATH = os.getenv("GOOGLE_CALENDAR_CREDENTIALS_PATH", "./credentials.json")
GOOGLE_CALENDAR_TOKEN_PATH = os.getenv("GOOGLE_CALENDAR_TOKEN_PATH", "./token.json")
GOOGLE_CALENDAR_ID = os.getenv("GOOGLE_CALENDAR_ID", "primary")

# Slack
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# Security
WEBHOOK_SECRET = os.getenv("WEBHOOK_SECRET", "")  # Required: secret token for webhook auth
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "")  # Required: password for dashboard access

# Dashboard
DASHBOARD_BASE_URL = os.getenv("DASHBOARD_BASE_URL", "https://gleadsy-reply-agent.onrender.com")

# Agent config
CONFIDENCE_THRESHOLD = float(os.getenv("CONFIDENCE_THRESHOLD", "0.4"))
MAX_REPLIES_PER_THREAD = int(os.getenv("MAX_REPLIES_PER_THREAD", "5"))
REPLY_COOLDOWN_HOURS = int(os.getenv("REPLY_COOLDOWN_HOURS", "4"))
TIMEZONE = os.getenv("TIMEZONE", "Europe/Vilnius")

# Email notifications
NOTIFY_EMAIL = os.getenv("NOTIFY_EMAIL", "")
GMAIL_APP_PASSWORD = os.getenv("GMAIL_APP_PASSWORD", "")

# Test mode - logs replies to Google Sheets instead of sending via Instantly
TEST_MODE = os.getenv("TEST_MODE", "false").lower() == "true"

# Auto-block: UNSUBSCRIBE klasifikacija -> Instantly blocklist + delete lead
# SAFEGUARD: Tik jei confidence >= UNSUBSCRIBE_CONFIDENCE_MIN (default 0.85)
AUTO_BLOCKLIST_UNSUBSCRIBE = os.getenv("AUTO_BLOCKLIST_UNSUBSCRIBE", "true").lower() == "true"
UNSUBSCRIBE_CONFIDENCE_MIN = float(os.getenv("UNSUBSCRIBE_CONFIDENCE_MIN", "0.85"))

# Lead attachment escalation: kai prospect'o reply turi prisegtu dokumentu (PDF, DOCX, Excel).
# Agent'as negali perskaityti turinio, todel eskaluoja Pauliui + skippina auto-reply.
# Itraukia vienkartini GET /api/v2/emails/{id} Instantly API call'a per webhook (cheap).
ENABLE_LEAD_DOCUMENT_ESCALATION = os.getenv("ENABLE_LEAD_DOCUMENT_ESCALATION", "true").lower() == "true"
