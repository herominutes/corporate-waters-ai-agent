"""Learning Engine — error pattern tracking and tool fitness metrics.

Inspired by OpenClaw Foundry's RISE/SelfEvolve/ADAS patterns.
Tracks recurring errors, successful fixes, and tool success rates
so Saul stops making the same mistakes.

Two systems:
1. Error Pattern Learning — normalize errors, track fixes, auto-suggest resolutions
2. Tool Fitness Tracking — success/failure rates per tool, flag underperformers

Data stored in SQLite (same DB as everything else).
"""

import asyncio
import json
import logging
import re
import time
from collections import Counter

import aiosqlite
from goodman import db

from goodman.config import DB_PATH

logger = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS error_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    signature TEXT NOT NULL UNIQUE,
    raw_example TEXT,
    occurrence_count INTEGER DEFAULT 1,
    last_seen TEXT DEFAULT (datetime('now')),
    resolution TEXT,
    resolution_count INTEGER DEFAULT 0,
    auto_promoted INTEGER DEFAULT 0,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS tool_metrics (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    tool_name TEXT NOT NULL,
    call_count INTEGER DEFAULT 0,
    success_count INTEGER DEFAULT 0,
    failure_count INTEGER DEFAULT 0,
    total_duration_ms INTEGER DEFAULT 0,
    last_error TEXT,
    last_used TEXT DEFAULT (datetime('now')),
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(tool_name)
);

CREATE INDEX IF NOT EXISTS idx_error_sig ON error_patterns(signature);
CREATE INDEX IF NOT EXISTS idx_tool_name ON tool_metrics(tool_name);
"""


async def init_db():
    """Create learning tables."""
    async with db.get_conn() as conn:
        await conn.executescript(SCHEMA)
        await conn.commit()
    logger.info("Learning engine tables initialized")


# ------------------------------------------------------------------
# Error Pattern Learning
# ------------------------------------------------------------------

def normalize_error(error_text: str) -> str:
    """Normalize an error message to a reusable signature.

    Strips numbers, file paths, UUIDs, and string literals so
    'ENOENT: /home/user/file.txt' and 'ENOENT: /tmp/other.txt'
    become the same pattern.
    """
    sig = error_text[:500]  # Cap length
    # Strip file paths
    sig = re.sub(r'/[\w./-]+', 'PATH', sig)
    # Strip UUIDs
    sig = re.sub(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', 'UUID', sig)
    # Strip numbers
    sig = re.sub(r'\b\d+\b', '#', sig)
    # Strip quoted strings
    sig = re.sub(r"'[^']*'", 'STR', sig)
    sig = re.sub(r'"[^"]*"', 'STR', sig)
    # Collapse whitespace
    sig = re.sub(r'\s+', ' ', sig).strip()
    return sig


# Known error → resolution mappings (bootstrapped)
KNOWN_RESOLUTIONS = {
    "invalid_grant: Token has been expired or revoked":
        "OAuth token expired. Run the OAuth re-auth flow: generate auth URL → user approves → exchange code for new tokens → save to credentials/oauth-tokens/",
    "Illegal header value":
        "API key contains newline or is duplicated in .env file. Check .env for duplicate OPENAI_API_KEY lines.",
    "proc.wait() timed out":
        "Claude CLI subprocess didn't exit cleanly. _kill_process_tree() should handle this. Check for orphaned node processes.",
    "overloaded_error":
        "Anthropic API overloaded (529). Transient — retry after 10-30 seconds. Brain.py auto-retries 3 times.",
    "529":
        "Anthropic API overloaded (529). Transient — retry after 10-30 seconds.",
    "insufficient_quota":
        "OpenAI API quota exceeded. Check billing at platform.openai.com. Consider switching to a different API key.",
    "# Unauthorized":
        "API key is invalid or expired. Check the API key in .env and verify it works with a direct curl test.",
    "CAPTCHA solve failed":
        "CAPTCHA image extraction returned invalid data. The page may not have loaded fully or the image selector changed.",
    "TimeoutError: Page.goto: Timeout":
        "Browser page load timed out. The site may be blocking the VPS IP. Try WARP proxy or a different approach.",
}


async def record_error(error_text: str, context: str = ""):
    """Record an error occurrence. Auto-matches known resolutions."""
    sig = normalize_error(error_text)
    if not sig or len(sig) < 5:
        return

    # Check known resolutions
    resolution = None
    for pattern, fix in KNOWN_RESOLUTIONS.items():
        if pattern.lower() in error_text.lower():
            resolution = fix
            break

    async with db.get_conn() as conn:
        cursor = await conn.execute(
            "SELECT id, occurrence_count, resolution FROM error_patterns WHERE signature = ?",
            (sig,),
        )
        row = await cursor.fetchone()

        if row:
            eid, count, existing_resolution = row
            # Update count and last_seen
            update_fields = {
                "occurrence_count": count + 1,
                "last_seen": "datetime('now')",
                "raw_example": error_text[:500],
            }
            # Only overwrite resolution if we have a known one and existing is empty
            if resolution and not existing_resolution:
                update_fields["resolution"] = resolution

            await conn.execute(
                f"UPDATE error_patterns SET occurrence_count = ?, last_seen = datetime('now'), "
                f"raw_example = ?" + (", resolution = ?" if resolution and not existing_resolution else "") +
                " WHERE id = ?",
                (count + 1, error_text[:500]) +
                ((resolution,) if resolution and not existing_resolution else ()) +
                (eid,),
            )
        else:
            await conn.execute(
                "INSERT INTO error_patterns (signature, raw_example, resolution) VALUES (?, ?, ?)",
                (sig, error_text[:500], resolution),
            )
        await conn.commit()


async def record_resolution(error_text: str, resolution: str):
    """Record a successful resolution for an error pattern."""
    sig = normalize_error(error_text)
    async with db.get_conn() as conn:
        await conn.execute(
            "UPDATE error_patterns SET resolution = ?, resolution_count = resolution_count + 1 "
            "WHERE signature = ?",
            (resolution[:500], sig),
        )
        await conn.commit()


async def get_resolution(error_text: str) -> str | None:
    """Look up a known resolution for an error. Returns None if unknown."""
    sig = normalize_error(error_text)

    async with db.get_conn() as conn:
        cursor = await conn.execute(
            "SELECT resolution FROM error_patterns WHERE signature = ? AND resolution IS NOT NULL",
            (sig,),
        )
        row = await cursor.fetchone()
        if row and row[0]:
            return row[0]

    # Try known resolutions directly
    for pattern, fix in KNOWN_RESOLUTIONS.items():
        if pattern.lower() in error_text.lower():
            return fix

    return None


async def get_error_insights() -> str:
    """Get insights about recurring errors for the heartbeat/context."""
    async with db.get_conn() as conn:
        conn.row_factory = aiosqlite.Row

        # Top recurring unresolved errors
        cursor = await conn.execute(
            "SELECT signature, occurrence_count, raw_example FROM error_patterns "
            "WHERE resolution IS NULL AND occurrence_count >= 3 "
            "ORDER BY occurrence_count DESC LIMIT 5"
        )
        unresolved = await cursor.fetchall()

        # Recently resolved patterns (might be useful context)
        cursor = await conn.execute(
            "SELECT signature, resolution, resolution_count FROM error_patterns "
            "WHERE resolution IS NOT NULL AND resolution_count >= 2 "
            "ORDER BY resolution_count DESC LIMIT 5"
        )
        resolved = await cursor.fetchall()

    if not unresolved and not resolved:
        return ""

    lines = []
    if unresolved:
        lines.append("RECURRING UNRESOLVED ERRORS:")
        for r in unresolved:
            lines.append(f"  [{r['occurrence_count']}x] {r['signature'][:100]}")

    if resolved:
        lines.append("KNOWN ERROR FIXES:")
        for r in resolved:
            lines.append(f"  {r['signature'][:80]} → {r['resolution'][:100]}")

    return "\n".join(lines)


# ------------------------------------------------------------------
# Tool Fitness Tracking
# ------------------------------------------------------------------

async def record_tool_call(tool_name: str, success: bool, duration_ms: int = 0,
                           error: str = ""):
    """Record a tool call outcome."""
    async with db.get_conn() as conn:
        cursor = await conn.execute(
            "SELECT id, call_count, success_count, failure_count, total_duration_ms "
            "FROM tool_metrics WHERE tool_name = ?",
            (tool_name,),
        )
        row = await cursor.fetchone()

        if row:
            tid, calls, successes, failures, total_dur = row
            await conn.execute(
                "UPDATE tool_metrics SET call_count = ?, success_count = ?, "
                "failure_count = ?, total_duration_ms = ?, last_used = datetime('now')"
                + (", last_error = ?" if error else "") +
                " WHERE id = ?",
                (calls + 1, successes + (1 if success else 0),
                 failures + (0 if success else 1), total_dur + duration_ms) +
                ((error[:300],) if error else ()) +
                (tid,),
            )
        else:
            await conn.execute(
                "INSERT INTO tool_metrics (tool_name, call_count, success_count, "
                "failure_count, total_duration_ms, last_error) VALUES (?, ?, ?, ?, ?, ?)",
                (tool_name, 1, 1 if success else 0, 0 if success else 1,
                 duration_ms, error[:300] if error else None),
            )
        await conn.commit()


async def get_tool_fitness() -> list[dict]:
    """Get fitness metrics for all tracked tools."""
    async with db.get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT tool_name, call_count, success_count, failure_count, "
            "total_duration_ms, last_error, last_used FROM tool_metrics "
            "WHERE call_count >= 3 ORDER BY call_count DESC"
        )
        rows = await cursor.fetchall()

    results = []
    for r in rows:
        calls = r["call_count"]
        success_rate = r["success_count"] / calls if calls > 0 else 0
        avg_duration = r["total_duration_ms"] / calls if calls > 0 else 0
        fitness = success_rate  # Simple fitness = success rate

        results.append({
            "tool": r["tool_name"],
            "calls": calls,
            "success_rate": round(success_rate * 100, 1),
            "avg_duration_ms": round(avg_duration),
            "fitness": round(fitness * 100, 1),
            "last_error": r["last_error"],
            "flagged": fitness < 0.4 and calls >= 5,  # Flag if < 40% after 5+ calls
        })

    return results


async def get_fitness_context() -> str:
    """Get tool fitness summary for context injection."""
    metrics = await get_tool_fitness()
    if not metrics:
        return ""

    flagged = [m for m in metrics if m["flagged"]]
    if not flagged:
        return ""

    lines = ["UNDERPERFORMING TOOLS (< 40% success rate):"]
    for m in flagged:
        lines.append(
            f"  {m['tool']}: {m['success_rate']}% success ({m['calls']} calls). "
            f"Last error: {m['last_error'][:80] if m['last_error'] else 'n/a'}"
        )
    lines.append("Consider alternative approaches for these tools.")
    return "\n".join(lines)


# ------------------------------------------------------------------
# CLI
# ------------------------------------------------------------------

async def _cli_async():
    import sys
    args = sys.argv[1:]
    if not args:
        print("Usage: learning errors|fitness|resolve|insights")
        return

    await init_db()
    cmd = args[0].lower()

    if cmd == "errors":
        async with db.get_conn() as conn:
            conn.row_factory = aiosqlite.Row
            cursor = await conn.execute(
                "SELECT signature, occurrence_count, resolution, resolution_count "
                "FROM error_patterns ORDER BY occurrence_count DESC LIMIT 20"
            )
            rows = await cursor.fetchall()
            print(f"=== {len(rows)} error patterns ===")
            for r in rows:
                status = f"FIX: {r['resolution'][:60]}" if r['resolution'] else "UNRESOLVED"
                print(f"  [{r['occurrence_count']}x] {r['signature'][:80]}")
                print(f"       {status}")

    elif cmd == "fitness":
        metrics = await get_tool_fitness()
        if not metrics:
            print("No tool metrics yet (need 3+ calls)")
            return
        print(f"=== Tool Fitness ({len(metrics)} tools) ===")
        for m in metrics:
            flag = " *** FLAGGED ***" if m["flagged"] else ""
            print(f"  {m['tool']:30s} {m['success_rate']:5.1f}% success "
                  f"({m['calls']} calls, {m['avg_duration_ms']}ms avg){flag}")

    elif cmd == "insights":
        insights = await get_error_insights()
        print(insights if insights else "No error insights yet.")

    elif cmd == "resolve" and len(args) >= 3:
        error_sig = args[1]
        resolution = " ".join(args[2:])
        await record_resolution(error_sig, resolution)
        print(f"Resolution recorded for pattern: {error_sig[:50]}")

    else:
        print("Usage: learning errors|fitness|resolve <signature> <resolution>|insights")


def _cli():
    asyncio.run(_cli_async())


if __name__ == "__main__":
    _cli()
