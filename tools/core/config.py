"""Configuration loader for The Goodman Protocol."""

import json
import os
from pathlib import Path
from dotenv import load_dotenv

# Project root
PROJECT_ROOT = Path(__file__).parent.parent.parent
DATA_DIR = PROJECT_ROOT / "data"
LOGS_DIR = PROJECT_ROOT / "logs"

# Ensure dirs exist
DATA_DIR.mkdir(exist_ok=True)
LOGS_DIR.mkdir(exist_ok=True)

# Load .env
load_dotenv(PROJECT_ROOT / ".env")

# Telegram
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_ALLOWED_USER_ID = os.getenv("TELEGRAM_ALLOWED_USER_ID", "")

# Timezone. Owner (Telegram + main process) = Vilnius. Slack requesters live in Almaty,
# so brain.py sets CALLER_TIMEZONE=Asia/Almaty on their per-request subprocess env; that
# override (never present in the main/owner env) makes calendar ops + display resolve to
# Almaty for them. Same per-request-env isolation as BOARD_DATA_AUTHORIZED.
TIMEZONE = os.getenv("CALLER_TIMEZONE") or os.getenv("TIMEZONE", "Europe/Vilnius")

# Paths
DB_PATH = DATA_DIR / "goodman.db"
CLAUDE_CMD = os.getenv("CLAUDE_CMD", "claude")
MCP_CONFIG_PATH = PROJECT_ROOT / "mcp-config.json"
SERVICE_ACCOUNT_PATH = PROJECT_ROOT / "credentials" / "service-account.json"

# Gmail accounts (loaded from credentials/gmail-accounts.json)
GMAIL_ACCOUNTS_FILE = PROJECT_ROOT / "credentials" / "gmail-accounts.json"
if GMAIL_ACCOUNTS_FILE.exists():
    GMAIL_ACCOUNTS = json.loads(GMAIL_ACCOUNTS_FILE.read_text())
else:
    GMAIL_ACCOUNTS = []

# Slack
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN", "")
SLACK_APP_TOKEN = os.getenv("SLACK_APP_TOKEN", "")
SLACK_USER_TOKEN = os.getenv("SLACK_USER_TOKEN", "")

# Slack privileged users (comma-separated Slack user IDs who get full tool access)
SLACK_PRIVILEGED_USERS = [
    uid.strip() for uid in os.getenv("SLACK_PRIVILEGED_USERS", "").split(",") if uid.strip()
]
SLACK_OWNER_USER_ID = os.getenv("SLACK_OWNER_USER_ID", "")

# --- Privilege capability tiers (deterministic, per-user) -------------------
# A non-owner Slack caller may only exercise the capabilities their tier grants.
# Owner / internal (Telegram) callers always have ALL capabilities.
CAP_CALENDAR = "calendar"
CAP_EMAIL = "email"
CAP_SLACK = "slack"
CAP_HUNTFLOW = "demoats"
CAP_CONFLUENCE = "confluence"
CAP_TRELLO = "trello"
ALL_CAPS = {CAP_CALENDAR, CAP_EMAIL, CAP_SLACK, CAP_HUNTFLOW, CAP_CONFLUENCE, CAP_TRELLO}

TIER_CAPS = {
    "full":      set(ALL_CAPS),                       # current surface (still hard-gated by #1-#3)
    "standard":  ALL_CAPS - {CAP_HUNTFLOW},           # full tool surface EXCEPT DemoATS (default)
    "comms":     {CAP_CALENDAR, CAP_EMAIL, CAP_SLACK},
    "recruiter": {CAP_HUNTFLOW, CAP_CALENDAR},
    "scheduler": {CAP_CALENDAR},
    "readonly":  set(),
}
# Default tier for a privileged user not explicitly mapped. Defaults to "full" so existing
# workflows don't break the instant this ships; tighten per-user via SLACK_USER_TIERS, or
# lower the global floor with SLACK_DEFAULT_TIER (e.g. "scheduler").
SLACK_DEFAULT_TIER = os.getenv("SLACK_DEFAULT_TIER", "full")
# Per-user tier assignments: "U123ABC:recruiter,U456DEF:scheduler"
SLACK_USER_TIERS = {}
for _pair in os.getenv("SLACK_USER_TIERS", "").split(","):
    if ":" in _pair:
        _u, _t = _pair.split(":", 1)
        SLACK_USER_TIERS[_u.strip()] = _t.strip()


def caps_for_user(uid: str) -> set:
    """Capabilities granted to a Slack user id (owner = all)."""
    if uid and uid == SLACK_OWNER_USER_ID:
        return set(ALL_CAPS)
    tier = SLACK_USER_TIERS.get(uid, SLACK_DEFAULT_TIER)
    return set(TIER_CAPS.get(tier, set()))


def caller_caps() -> set:
    """Read inside a tool subprocess. SLACK_CALLER_CAPS is set by brain.py for non-owner
    callers (possibly empty = no action caps). Absent entirely = owner/internal = all caps."""
    raw = os.getenv("SLACK_CALLER_CAPS")
    if raw is None:
        return set(ALL_CAPS)
    return {c.strip() for c in raw.split(",") if c.strip()}


def caller_has(cap: str) -> bool:
    return cap in caller_caps()


# --- Scoped send-as delegation (one user → sends from their OWN mailbox) -----
# Maps a Slack user id → the email address Saul may send AS on their behalf (their own
# inbox). Bound to that user only; the mailbox is invisible to every other caller.
# Used e.g. for a recruiter sending rejection letters from her own address.
#   SLACK_SEND_AS_ACCOUNTS="U_MARIA:maria.sokolova@acme-corp.example"
SLACK_SEND_AS_ACCOUNTS = {}
for _pair in os.getenv("SLACK_SEND_AS_ACCOUNTS", "").split(","):
    if ":" in _pair:
        _u, _e = _pair.split(":", 1)
        SLACK_SEND_AS_ACCOUNTS[_u.strip()] = _e.strip().lower()
# Delegated mailboxes — hidden from all callers except their mapped owner.
DELEGATED_SEND_ACCOUNTS = set(SLACK_SEND_AS_ACCOUNTS.values())


def send_as_for_user(uid: str) -> str:
    """The mailbox a Slack user may have Saul send AS (their own), or '' if none."""
    return SLACK_SEND_AS_ACCOUNTS.get(uid, "")


# Board-data allowlist — ONLY these Slack user IDs may have Saul read the
# confidential Core Classifieds Business Case + ФОТ sheets (board_sheets_cli).
# Strictly narrower than privileged users. Default = the four authorized execs
# (Pavlov, Belov, Chernov, Chen).
SLACK_BOARD_DATA_USERS = [
    uid.strip() for uid in os.getenv(
        "SLACK_BOARD_DATA_USERS",
        "U000000001,U000000002,U000000003,U000000004",
    ).split(",") if uid.strip()
]

# Fernet key for the encrypted board docs (data/board/restricted/*.enc). Loaded from
# a 600-mode FILE into a Python constant — deliberately NOT via os.environ, so it is
# never copied into the subprocess env for non-authorized users. brain.py injects it
# into the brain subprocess env ONLY when board_authorized (alongside the auth flag).
try:
    BOARD_DATA_KEY = (PROJECT_ROOT / "credentials" / "board_data.key").read_text().strip()
except Exception:
    BOARD_DATA_KEY = ""

# Restricted Slack channels — only privileged users may read/search these
# Comma-separated channel names (without #). Content from these channels is
# stripped from search results and blocked from get_channel_messages for
# non-privileged callers.
SLACK_RESTRICTED_CHANNELS = [
    ch.strip().lower().lstrip("#")
    for ch in os.getenv("SLACK_RESTRICTED_CHANNELS", "exec,product-heads").split(",")
    if ch.strip()
]

# Trello
TRELLO_API_KEY = os.getenv("TRELLO_API_KEY", "")
TRELLO_TOKEN = os.getenv("TRELLO_TOKEN", "")
TRELLO_BOARD_ID = os.getenv("TRELLO_BOARD_ID", "")

# IBKR Flex Web Service (read-only portfolio)
IBKR_FLEX_TOKEN = os.getenv("IBKR_FLEX_TOKEN", "")
IBKR_FLEX_QUERY_ID = os.getenv("IBKR_FLEX_QUERY_ID", "")

# DemoATS ATS
HUNTFLOW_BASE_URL = os.getenv("HUNTFLOW_BASE_URL", "https://api.demoats.ai")
HUNTFLOW_ACCOUNT_ID = os.getenv("HUNTFLOW_ACCOUNT_ID", "")
HUNTFLOW_ACCESS_TOKEN = os.getenv("HUNTFLOW_ACCESS_TOKEN", "")
HUNTFLOW_REFRESH_TOKEN = os.getenv("HUNTFLOW_REFRESH_TOKEN", "")
HUNTFLOW_UI_BASE = os.getenv("HUNTFLOW_UI_BASE", "https://demoats.ai")

# Travelpayouts Flight Data API (real flight prices for travel queries)
TRAVELPAYOUTS_TOKEN = os.getenv("TRAVELPAYOUTS_TOKEN", "")

# Google Books API key (OPTIONAL — Google Books works without one; a key only
# raises the daily quota). Open Library needs no key.
GOOGLE_BOOKS_API_KEY = os.getenv("GOOGLE_BOOKS_API_KEY", "")

# Granola
GRANOLA_ENABLED = (PROJECT_ROOT / "credentials" / "granola-tokens.json").exists()

# Voice transcription
WHISPER_LANGUAGE = os.getenv("WHISPER_LANGUAGE", "auto")

# Chatterbox TTS (primary — via Tailscale)
CHATTERBOX_URL = os.getenv("CHATTERBOX_URL", "http://100.105.221.79:4123")
CHATTERBOX_VOICE = os.getenv("CHATTERBOX_VOICE", "saul")

# ElevenLabs TTS (fallback)
ELEVENLABS_API_KEY = os.getenv("ELEVENLABS_API_KEY", "")
ELEVENLABS_VOICE_ID = os.getenv("ELEVENLABS_VOICE_ID", "")
TTS_ENABLED = bool(CHATTERBOX_URL or (ELEVENLABS_API_KEY and ELEVENLABS_VOICE_ID))

# Telethon userbot (personal TG DM listener)
TELETHON_API_ID = os.getenv("TELETHON_API_ID", "")
TELETHON_API_HASH = os.getenv("TELETHON_API_HASH", "")
TELETHON_SESSION_PATH = os.getenv("TELETHON_SESSION_PATH",
                                   str(PROJECT_ROOT / "credentials" / "telethon"))
TELETHON_IGNORE_USERS = [
    uid.strip() for uid in os.getenv("TELETHON_IGNORE_USERS", "").split(",") if uid.strip()
]
# Always ignore the bot itself (extracted from token prefix)
if TELEGRAM_BOT_TOKEN and ":" in TELEGRAM_BOT_TOKEN:
    _bot_uid = TELEGRAM_BOT_TOKEN.split(":")[0]
    if _bot_uid not in TELETHON_IGNORE_USERS:
        TELETHON_IGNORE_USERS.append(_bot_uid)
TELETHON_IGNORE_USERNAMES = [
    u.strip().lower().lstrip("@")
    for u in os.getenv("TELETHON_IGNORE_USERNAMES", "").split(",") if u.strip()
]

# Telegram group monitoring
TELEGRAM_GROUP_KEYWORDS = [
    kw.strip().lower()
    for kw in os.getenv("TELEGRAM_GROUP_KEYWORDS", "").split(",") if kw.strip()
]
TELEGRAM_GROUP_NOTIFY_MENTIONS = os.getenv(
    "TELEGRAM_GROUP_NOTIFY_MENTIONS", "true"
).lower() in ("1", "true", "yes")

# Consulate monitor (passport appointment checker)
CONSULATE_MONITOR_ENABLED = os.getenv("CONSULATE_MONITOR_ENABLED", "true").lower() in ("1", "true", "yes")

# WhatsApp sidecar
WA_ENABLED = os.getenv("WA_ENABLED", "").lower() in ("1", "true", "yes")
WA_WS_URL = os.getenv("WA_WS_URL", "ws://localhost:8082")

# Vision (OpenAI GPT-4o-mini)
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")

# Gemini (second-opinion for deep legal/research)
GEMINI_API_KEY = os.getenv("GEMINI_API_KEY", "")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-3.1-pro-preview")

# Confluence (Atlassian Cloud)
CONFLUENCE_BASE_URL = os.getenv("CONFLUENCE_BASE_URL", "")
CONFLUENCE_EMAIL = os.getenv("CONFLUENCE_EMAIL", "")
CONFLUENCE_API_TOKEN = os.getenv("CONFLUENCE_API_TOKEN", "")
CONFLUENCE_DEFAULT_SPACE = os.getenv("CONFLUENCE_DEFAULT_SPACE", "")

# API server (for VisionClaw / external integrations)
API_PORT = int(os.getenv("API_PORT", "8081"))
API_HOST = os.getenv("API_HOST", "0.0.0.0")
API_SECRET = os.getenv("API_SECRET", "")
