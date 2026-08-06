"""Session Management — conversation boundary detection and lifecycle.

Inspired by OpenClaw's session architecture. Sessions provide natural
conversation boundaries so that a 9am tax discussion doesn't bleed into
a 9pm dinner reservation request.

Session lifecycle:
  1. Auto-create on first message after reset (daily or idle timeout)
  2. Reuse current session until it expires
  3. On expiry: auto-summarize the session and archive it
  4. New messages start a fresh session with clean context

Reset modes:
  - DAILY: Reset at a configurable hour (default 4am user timezone)
  - IDLE: Reset after N minutes of silence (default 120 min)
  - MANUAL: User sends /reset command

Context loading:
  - Current session: full message history
  - Recent sessions: summaries only (last 5)
  - Permanent memories: always loaded (filtered by relevance later)
"""

import logging
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import aiosqlite

from goodman import db
from goodman.config import DB_PATH, TIMEZONE

logger = logging.getLogger(__name__)

# Session configuration
IDLE_TIMEOUT_MINUTES = 120     # Reset after 2 hours of silence
DAILY_RESET_HOUR = 4           # Reset at 4am user timezone
MAX_SESSION_MESSAGES = 100     # Force-close session after this many messages


# ------------------------------------------------------------------
# Database schema extension
# ------------------------------------------------------------------

SESSION_SCHEMA = """
CREATE TABLE IF NOT EXISTS sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    message_count INTEGER DEFAULT 0,
    summary TEXT,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'closed'))
);

CREATE INDEX IF NOT EXISTS idx_sessions_user_status
    ON sessions(user_id, status);
CREATE INDEX IF NOT EXISTS idx_sessions_user_started
    ON sessions(user_id, started_at DESC);
"""


async def init_session_db():
    """Create session tables if they don't exist."""
    async with db.get_conn() as conn:
        await conn.executescript(SESSION_SCHEMA)
        await conn.commit()
    logger.info("Session database initialized")


# ------------------------------------------------------------------
# Session lifecycle
# ------------------------------------------------------------------

async def get_active_session(user_id: str) -> dict | None:
    """Get the currently active session for a user, if any."""
    async with db.get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT id, user_id, started_at, ended_at, message_count, summary, status "
            "FROM sessions WHERE user_id = ? AND status = 'active' "
            "ORDER BY started_at DESC LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        return dict(row) if row else None


async def create_session(user_id: str) -> int:
    """Create a new active session. Returns session ID."""
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    async with db.get_conn() as conn:
        cursor = await conn.execute(
            "INSERT INTO sessions (user_id, started_at, status) VALUES (?, ?, 'active')",
            (user_id, now),
        )
        await conn.commit()
        session_id = cursor.lastrowid
        logger.info("Created session %d for user %s", session_id, user_id)
        return session_id


async def close_session(user_id: str, session_id: int, summary: str | None = None):
    """Close a session, optionally with a summary."""
    now = datetime.now(ZoneInfo(TIMEZONE)).isoformat()
    async with db.get_conn() as conn:
        await conn.execute(
            "UPDATE sessions SET status = 'closed', ended_at = ?, summary = ? WHERE id = ?",
            (now, summary, session_id),
        )
        await conn.commit()
    logger.info("Closed session %d for user %s", session_id, user_id)


async def increment_session_messages(session_id: int):
    """Increment the message count for a session."""
    async with db.get_conn() as conn:
        await conn.execute(
            "UPDATE sessions SET message_count = message_count + 1 WHERE id = ?",
            (session_id,),
        )
        await conn.commit()


async def get_session_summaries(user_id: str, limit: int = 5) -> list[dict]:
    """Get the most recent closed session summaries."""
    async with db.get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT id, started_at, ended_at, message_count, summary "
            "FROM sessions WHERE user_id = ? AND status = 'closed' AND summary IS NOT NULL "
            "ORDER BY started_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in reversed(rows)]


# ------------------------------------------------------------------
# Session boundary detection
# ------------------------------------------------------------------

def should_reset_session(session: dict | None) -> tuple[bool, str]:
    """Determine if the current session should be reset.

    Returns (should_reset, reason).
    """
    if session is None:
        return True, "no_active_session"

    now = datetime.now(ZoneInfo(TIMEZONE))

    # Parse session start time
    started_at = session["started_at"]
    try:
        if started_at.endswith("+00:00") or started_at.endswith("Z"):
            session_start = datetime.fromisoformat(started_at.replace("Z", "+00:00"))
        else:
            session_start = datetime.fromisoformat(started_at)
            if session_start.tzinfo is None:
                session_start = session_start.replace(tzinfo=ZoneInfo(TIMEZONE))
    except (ValueError, TypeError):
        return True, "invalid_session_time"

    # Check idle timeout
    # We use the session start + (message_count * estimated_interval) as a proxy
    # for last activity, but it's better to check the actual last message time
    # For simplicity, we'll use a separate check in the caller
    # Here we just check the daily reset boundary

    # Daily reset: if we crossed the reset hour since session started
    reset_time_today = now.replace(
        hour=DAILY_RESET_HOUR, minute=0, second=0, microsecond=0
    )
    if session_start < reset_time_today <= now:
        return True, "daily_reset"

    # Message count limit
    msg_count = session.get("message_count", 0)
    if msg_count >= MAX_SESSION_MESSAGES:
        return True, "message_limit"

    return False, "session_active"


async def check_idle_timeout(user_id: str, session: dict) -> bool:
    """Check if the session has been idle too long based on last message time."""
    async with db.get_conn() as conn:
        cursor = await conn.execute(
            "SELECT created_at FROM conversations WHERE user_id = ? "
            "ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        )
        row = await cursor.fetchone()
        if not row:
            return False

        last_msg_time_str = row[0]
        try:
            last_msg = datetime.fromisoformat(last_msg_time_str)
            if last_msg.tzinfo is None:
                last_msg = last_msg.replace(tzinfo=ZoneInfo("UTC"))
        except (ValueError, TypeError):
            return False

        now = datetime.now(ZoneInfo("UTC"))
        idle_minutes = (now - last_msg).total_seconds() / 60

        if idle_minutes > IDLE_TIMEOUT_MINUTES:
            logger.info(
                "Session %d idle for %.0f minutes (limit: %d), resetting",
                session["id"], idle_minutes, IDLE_TIMEOUT_MINUTES,
            )
            return True

    return False


# ------------------------------------------------------------------
# Session-aware message flow
# ------------------------------------------------------------------

async def ensure_session(user_id: str) -> int:
    """Ensure an active session exists, creating or rotating as needed.

    Returns the active session ID.
    """
    session = await get_active_session(user_id)

    should_reset, reason = should_reset_session(session)

    # Additional idle check
    if not should_reset and session:
        is_idle = await check_idle_timeout(user_id, session)
        if is_idle:
            should_reset = True
            reason = "idle_timeout"

    if should_reset and session:
        # Close the old session with auto-summary
        summary = await _auto_summarize_session(user_id, session)
        await close_session(user_id, session["id"], summary)
        logger.info("Session %d closed: %s", session["id"], reason)

    if should_reset:
        return await create_session(user_id)

    return session["id"]


async def _auto_summarize_session(user_id: str, session: dict) -> str | None:
    """Auto-summarize a session's conversation before closing it.

    Uses a lightweight Claude call to create a fact-dense summary.
    Falls back to a basic extraction if Claude call fails.
    """
    from goodman import brain

    # Get messages from this session's time range
    async with db.get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT role, content FROM conversations "
            "WHERE user_id = ? AND created_at >= ? "
            "ORDER BY created_at ASC LIMIT 50",
            (user_id, session["started_at"]),
        )
        rows = await cursor.fetchall()

    if not rows or len(rows) < 2:
        return None

    conv_text = ""
    for row in rows:
        prefix = "User" if row["role"] == "user" else "Saul"
        content = row["content"][:500]  # Truncate long messages
        conv_text += f"{prefix}: {content}\n"

    # Try Claude summarization first
    try:
        summary = await brain._call_claude(
            brain.SUMMARIZE_PROMPT,
            conv_text[:6000],
            max_turns=0,
            timeout=30,
        )
        if summary and not summary.startswith("Error:"):
            return summary[:800]
    except Exception as e:
        logger.warning("Session summary Claude call failed: %s", e)

    # Fallback: extract key info directly from messages
    try:
        user_msgs = [r["content"][:200] for r in rows if r["role"] == "user"]
        if user_msgs:
            # Take first 3 and last 2 user messages as topic indicators
            snippets = user_msgs[:3]
            if len(user_msgs) > 3:
                snippets.extend(user_msgs[-2:])
            topics = "; ".join(s.strip()[:100] for s in snippets)
            return f"Session ({len(rows)} messages). User topics: {topics}"[:800]
    except Exception as e:
        logger.warning("Session summary fallback failed: %s", e)

    return f"Session with {len(rows)} messages (summary generation failed)"


async def get_session_messages(user_id: str, session_id: int, limit: int = 50) -> list[dict]:
    """Get messages belonging to a specific session."""
    # Get session start time
    session = None
    async with db.get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT started_at FROM sessions WHERE id = ?", (session_id,)
        )
        row = await cursor.fetchone()
        if row:
            session = dict(row)

    if not session:
        return []

    async with db.get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT id, role, content, created_at FROM conversations "
            "WHERE user_id = ? AND created_at >= ? "
            "ORDER BY created_at DESC LIMIT ?",
            (user_id, session["started_at"], limit),
        )
        rows = await cursor.fetchall()
        return [
            {"id": row["id"], "role": row["role"], "content": row["content"],
             "created_at": row["created_at"]}
            for row in reversed(rows)
        ]


async def reset_session(user_id: str) -> int:
    """Manually reset the session (e.g., from /reset command).

    Closes current session and starts a new one.
    Returns the new session ID.
    """
    session = await get_active_session(user_id)
    if session:
        summary = await _auto_summarize_session(user_id, session)
        await close_session(user_id, session["id"], summary)
        logger.info("Manual session reset for user %s", user_id)

    return await create_session(user_id)
