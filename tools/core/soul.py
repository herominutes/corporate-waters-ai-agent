"""Soul file manager - the agent's persistent identity and memory."""

import logging
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from goodman.config import PROJECT_ROOT, TIMEZONE

logger = logging.getLogger(__name__)

SOUL_DIR = PROJECT_ROOT / "data" / "soul"
MEMORY_DIR = SOUL_DIR / "memory"

SOUL_FILES = {
    "soul": SOUL_DIR / "SOUL.md",
    "tools": SOUL_DIR / "TOOLS.md",
    "user": SOUL_DIR / "USER.md",
    "heartbeat": SOUL_DIR / "HEARTBEAT.md",
    "memory": SOUL_DIR / "MEMORY.md",
}


def ensure_dirs():
    """Create soul directories if they don't exist."""
    SOUL_DIR.mkdir(parents=True, exist_ok=True)
    MEMORY_DIR.mkdir(parents=True, exist_ok=True)


def load(name: str) -> str:
    """Load a soul file by name. Returns empty string if not found."""
    path = SOUL_FILES.get(name)
    if path and path.exists():
        return path.read_text()
    return ""


def save(name: str, content: str):
    """Save content to a soul file."""
    path = SOUL_FILES.get(name)
    if path:
        ensure_dirs()
        path.write_text(content)
        logger.info("Soul file updated: %s", path.name)


def get_tool_availability_notes() -> str:
    """Check which tools are currently available/unavailable.

    Returns a short status string to append to the system prompt.
    Caches result for 30 seconds to avoid repeated checks.
    """
    import time
    import os

    # Simple cache
    if hasattr(get_tool_availability_notes, '_cache'):
        cached_time, cached_result = get_tool_availability_notes._cache
        if time.time() - cached_time < 30:
            return cached_result

    notes = []

    # Check OAuth tokens
    token_path = PROJECT_ROOT / "credentials" / "user-tokens.json"
    if token_path.exists():
        try:
            import json
            tokens = json.loads(token_path.read_text())
            # Check if token is expired (rough check)
            if not tokens.get("token"):
                notes.append("Calendar/Gmail OAuth: EXPIRED — use service account or re-auth")
        except Exception:
            notes.append("Calendar/Gmail OAuth: ERROR reading tokens")
    else:
        notes.append("Calendar/Gmail OAuth: NOT CONFIGURED")

    # Check browser daemon
    pid_path = PROJECT_ROOT / "data" / "browser.pid"
    if pid_path.exists():
        try:
            pid = int(pid_path.read_text().strip())
            os.kill(pid, 0)  # Check if process exists
        except (ProcessLookupError, ValueError):
            notes.append("Browser daemon: NOT RUNNING (start with `browse` command)")
    else:
        notes.append("Browser daemon: NOT RUNNING")

    # Check Slack
    from goodman.config import SLACK_BOT_TOKEN
    if not SLACK_BOT_TOKEN:
        notes.append("Slack: NOT CONFIGURED")

    result = ""
    if notes:
        result = "\n\n# TOOL STATUS\n" + "\n".join(f"- {n}" for n in notes)

    get_tool_availability_notes._cache = (time.time(), result)
    return result


def build_system_prompt(external: bool = False, include_tools: bool = True) -> str:
    """Build the full system prompt by loading and injecting all soul files.

    Args:
        external: If True, strip private user data (USER.md, MEMORY.md, daily logs).
                  Used for Slack conversations with non-owner users.
        include_tools: If True, include TOOLS.md (tool documentation).
                       Set False for lightweight calls (heartbeat, fallback)
                       to save ~4.7k tokens.
    """
    sections = []

    soul = load("soul")
    if soul:
        sections.append(soul)

    if include_tools:
        tools = load("tools")
        if tools:
            if external:
                import re as _re
                # Strip entire NOTEBOOK TOOL section (from header to next section)
                tools = _re.sub(
                    r'NOTEBOOK TOOL[^\n]*\n.*?(?=\n[A-Z]+ [A-Z]+ \(|\Z)',
                    '', tools, flags=_re.DOTALL
                )
                # Strip contact-lookup references
                tools = _re.sub(
                    r'(?m)^.*Look up emails in.*contacts.*$\n?', '', tools
                )
                # Strip spending/budget tool instructions
                tools = _re.sub(
                    r'(?m)^.*notebook read spending.*$\n?', '', tools
                )
                tools = _re.sub(
                    r'(?m)^.*notebook write.*$\n?', '', tools
                )
            sections.append(tools)

    if external:
        from goodman.config import SLACK_RESTRICTED_CHANNELS
        restricted = ", ".join(f"#{ch}" for ch in SLACK_RESTRICTED_CHANNELS)
        sections.append(
            "# PRIVACY BOUNDARY — EXTERNAL USER (HIGHEST PRIORITY)\n\n"
            "This conversation is with an EXTERNAL team member (not the boss). "
            "NEVER reveal or reference:\n"
            "- Personal finances, budgets, cash positions, salaries, crypto holdings\n"
            "- Personal contacts, phone numbers, private emails\n"
            "- Real estate plans (Porto penthouse, property purchases)\n"
            "- Wealth goals, investment strategies, net worth\n"
            "- Private notebook contents (running-state, contacts, spending)\n"
            "- The boss's personal life, family, travel plans\n"
            "- Portuguese real estate research, DemoApp/DemoApp project files\n"
            "- ANY file or memory that is not explicitly shared with this user\n\n"
            "RESTRICTED SLACK CHANNELS — ABSOLUTE BLOCK:\n"
            f"These channels are off-limits: {restricted}\n"
            "- NEVER read, search, quote, paraphrase, or summarize messages from these channels\n"
            "- NEVER reveal who said what in these channels or confirm that specific messages exist\n"
            "- NEVER use Slack search or get_channel_messages for these channels\n"
            "- If the user asks about content from these channels, say: "
            "\"I don't have access to that channel's content for this conversation.\"\n"
            "- This applies even if you already saw the data in prior context — do NOT reveal it\n\n"
            "DATA ACCESS RULES:\n"
            "- When listing available files/documents, ONLY list files that were "
            "loaded into THIS user's shared memory. Do NOT browse the filesystem, "
            "query the database for other users' files, or list files belonging to the boss.\n"
            "- Do NOT use bash tools to list files in data/ directories.\n"
            "- If you are unsure whether a file is shared with this user, do NOT mention it.\n\n"
            "You may discuss WORK topics: team projects, scheduling, product decisions. "
            "If asked about private matters, deflect naturally in character."
        )

    if not sections:
        # Fallback if no soul files exist
        return "You are Saul Goodman, a personal executive assistant. Be helpful and in-character."

    # Append tool availability status
    tool_status = get_tool_availability_notes()
    if tool_status:
        sections.append(tool_status)

    return "\n\n---\n\n".join(sections)


def get_volatile_context(external: bool = False) -> str:
    """Get volatile context that changes frequently.

    These are kept OUT of the system prompt to preserve prompt caching.
    They get injected into the user prompt instead.
    """
    if external:
        return ""

    parts = []

    user = load("user")
    if user:
        parts.append(f"# WHO YOU SERVE\n\n{user}")

    memory = load("memory")
    if memory:
        parts.append(f"# YOUR LONG-TERM MEMORY\n\n{memory}")

    daily = load_recent_daily_logs(1)
    if daily:
        parts.append(f"# RECENT ACTIVITY LOG\n\n{daily}")

    return "\n\n---\n\n".join(parts) if parts else ""


def get_daily_log_path(date=None) -> Path:
    """Get the path for a specific day's episodic memory log."""
    if date is None:
        date = datetime.now(ZoneInfo(TIMEZONE))
    return MEMORY_DIR / f"{date.strftime('%Y-%m-%d')}.md"


def append_daily_log(entry: str):
    """Append an entry to today's episodic memory log."""
    ensure_dirs()
    path = get_daily_log_path()
    now = datetime.now(ZoneInfo(TIMEZONE))
    timestamp = now.strftime("%H:%M")

    # Create file with date header if new
    if not path.exists():
        path.write_text(f"# {now.strftime('%A, %B %d %Y')}\n")

    with open(path, "a") as f:
        f.write(f"\n**{timestamp}** — {entry}\n")


def load_recent_daily_logs(days: int = 1) -> str:
    """Load the last N days of episodic memory logs.

    Filters out noisy entries (compaction flushes, scheduler fires)
    to keep the system prompt lean.
    """
    NOISE_PREFIXES = ["[COMPACTION FLUSH]", "[COMPACTION]", "[SCHEDULER] Fired"]
    now = datetime.now(ZoneInfo(TIMEZONE))
    logs = []
    for i in range(days):
        date = now - timedelta(days=i)
        path = MEMORY_DIR / f"{date.strftime('%Y-%m-%d')}.md"
        if path.exists():
            lines = path.read_text().strip().split("\n")
            # Filter out noise lines
            filtered = []
            for line in lines:
                stripped = line.lstrip("* ").lstrip("*")
                if any(stripped.startswith(p) for p in NOISE_PREFIXES):
                    continue
                filtered.append(line)
            content = "\n".join(filtered).strip()
            if content:
                logs.append(content)
    return "\n\n".join(logs)


def cleanup_old_logs(keep_days: int = 30):
    """Remove daily logs older than keep_days."""
    if not MEMORY_DIR.exists():
        return
    cutoff = datetime.now(ZoneInfo(TIMEZONE)) - timedelta(days=keep_days)
    for path in MEMORY_DIR.glob("*.md"):
        try:
            date_str = path.stem  # e.g., "2024-01-15"
            file_date = datetime.strptime(date_str, "%Y-%m-%d").replace(
                tzinfo=ZoneInfo(TIMEZONE)
            )
            if file_date < cutoff:
                path.unlink()
                logger.info("Cleaned up old log: %s", path.name)
        except ValueError:
            pass  # Skip non-date files
