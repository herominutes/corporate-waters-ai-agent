"""Conversation Search — full-text search across all past conversations.

Lets Saul search his own conversation history beyond the current session.
Uses SQLite LIKE for simplicity (fast enough for <100k rows).

CLI interface (used by Claude subprocess):
  convsearch <query>               Search conversations for keywords
  convsearch <query> --limit 20    Limit results (default 30)
  convsearch <query> --days 7      Limit to last N days
"""

import asyncio
import sys
from datetime import datetime, timedelta

import aiosqlite
from goodman import db

from goodman.config import DB_PATH


async def search_conversations(
    user_id: str,
    query: str,
    limit: int = 30,
    days: int | None = None,
) -> list[dict]:
    """Search conversation history using FTS5 full-text search.

    Falls back to LIKE-based search if FTS5 is not available.
    Returns newest matches first with BM25 ranking.
    """
    terms = [t.strip() for t in query.lower().split() if t.strip()]
    if not terms:
        return []

    async with db.get_conn() as conn:
        conn.row_factory = aiosqlite.Row

        # Try FTS5 first — two-step: pure MATCH then hydrate by rowid
        try:
            fts_query = " AND ".join(f'"{t}"' for t in terms)

            cursor = await conn.execute(
                "SELECT rowid, bm25(conversations_fts) AS rank "
                "FROM conversations_fts WHERE conversations_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, limit * 3),
            )
            fts_rows = await cursor.fetchall()
            if fts_rows:
                ids = [r["rowid"] for r in fts_rows]
                placeholders = ",".join("?" * len(ids))
                date_filter = ""
                params: list = [user_id] + ids
                if days:
                    cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
                    date_filter = "AND created_at >= ?"
                    params.append(cutoff)
                params.append(limit)

                cursor = await conn.execute(
                    f"SELECT role, content, created_at FROM conversations "
                    f"WHERE user_id = ? AND id IN ({placeholders}) {date_filter} "
                    f"LIMIT ?",
                    params,
                )
                rows = await cursor.fetchall()
                return [
                    {"role": row["role"], "content": row["content"],
                     "created_at": row["created_at"]}
                    for row in rows
                ]
            return []
        except Exception:
            pass  # FTS5 not available, fall back to LIKE

        # Fallback: LIKE-based search (original behavior)
        conditions = ["user_id = ?"]
        params_like: list = [user_id]

        for term in terms:
            conditions.append("LOWER(content) LIKE ?")
            params_like.append(f"%{term}%")

        if days:
            cutoff = (datetime.utcnow() - timedelta(days=days)).isoformat()
            conditions.append("created_at >= ?")
            params_like.append(cutoff)

        params_like.append(limit)
        where = " AND ".join(conditions)

        cursor = await conn.execute(
            f"SELECT role, content, created_at FROM conversations "
            f"WHERE {where} ORDER BY created_at DESC LIMIT ?",
            params_like,
        )
        rows = await cursor.fetchall()
        return [
            {"role": row["role"], "content": row["content"],
             "created_at": row["created_at"]}
            for row in rows
        ]


def _format_results(results: list[dict], max_chars: int = 8000) -> str:
    """Format search results for CLI output."""
    if not results:
        return "No matches found."

    lines = [f"Found {len(results)} matches:\n"]
    total_chars = 0

    for r in results:
        prefix = "User" if r["role"] == "user" else "Saul"
        timestamp = r["created_at"][:16] if r["created_at"] else "?"
        content = r["content"]
        # Truncate long messages
        if len(content) > 300:
            content = content[:300] + "..."
        line = f"[{timestamp}] {prefix}: {content}\n"
        if total_chars + len(line) > max_chars:
            lines.append(f"\n[... more results omitted, showing {len(lines)-1} of {len(results)} ...]")
            break
        lines.append(line)
        total_chars += len(line)

    return "\n".join(lines)


def _cli():
    """CLI entry point."""
    args = sys.argv[1:]
    if not args:
        print("Usage: convsearch <query> [--limit N] [--days N]")
        sys.exit(1)

    # Parse args
    query_parts = []
    limit = 30
    days = None
    i = 0
    while i < len(args):
        if args[i] == "--limit" and i + 1 < len(args):
            limit = int(args[i + 1])
            i += 2
        elif args[i] == "--days" and i + 1 < len(args):
            days = int(args[i + 1])
            i += 2
        else:
            query_parts.append(args[i])
            i += 1

    query = " ".join(query_parts)
    if not query:
        print("Usage: convsearch <query> [--limit N] [--days N]")
        sys.exit(1)

    # Need to figure out user_id — use the configured one
    from goodman.config import TELEGRAM_ALLOWED_USER_ID
    user_id = str(TELEGRAM_ALLOWED_USER_ID)

    results = asyncio.run(search_conversations(user_id, query, limit, days))
    print(_format_results(results))


if __name__ == "__main__":
    _cli()
