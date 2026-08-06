"""Canonical state layer — the single source of truth for MUTABLE current-state facts.

Fixes the recurring "Saul asserts a fact that used to be true" bug (held NBIS after
selling, stale employer, wrong contact email) by separating STATE from HISTORY:

  - STATE  (this module): entity attributes with a CURRENT value. Updates OVERWRITE
           (supersede) the prior value; reads are deterministic, never fuzzy similarity.
  - HISTORY (the `memories` table + ingested docs): episodic, append-only, date-framed,
           and EXCLUDED from current-state answers.

A holding, a contact email, an employer are all just (entity, attribute, value):
  set_state("NBIS", "holding", "sold — 0 shares", source="IBKR", valid_from="2026-05-22")
  get_state("NBIS", "holding")  -> {"value": "sold — 0 shares", ...}

Backed by `kg_state`, alongside the existing knowledge_graph tables (same DB).
"""
import asyncio
import logging
from datetime import datetime, timezone

import aiosqlite

from goodman.config import DB_PATH

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_state (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_key    TEXT NOT NULL,            -- normalized entity (lowercased): 'nbis', 'alex'
    entity_name   TEXT NOT NULL,            -- display name: 'NBIS', 'Alex'
    attribute     TEXT NOT NULL,            -- normalized: 'holding', 'email', 'employer'
    value         TEXT,                     -- current value as text
    valid_from    TEXT NOT NULL,            -- ISO date the value became true
    source        TEXT,                     -- provenance: 'broker', 'portfolio-db', 'user', ...
    superseded_at TEXT,                     -- NULL = CURRENT; set when overwritten
    created_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_kg_state_current
    ON kg_state(entity_key, attribute) WHERE superseded_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_kg_state_hist
    ON kg_state(entity_key, attribute, valid_from);
"""


def _norm(s: str) -> str:
    return (s or "").strip().lower()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


async def init_db():
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()


async def set_state(entity_name: str, attribute: str, value: str,
                    source: str = "user", valid_from: str | None = None) -> None:
    """Set the CURRENT value of an entity attribute, superseding any prior current value.

    This is the write primitive that makes corrections STICK: the old value is retired
    (superseded_at stamped), the new value becomes the single current row. History is kept.
    """
    ek, attr = _norm(entity_name), _norm(attribute)
    vf = valid_from or _now()
    async with aiosqlite.connect(DB_PATH) as db:
        # retire the existing current value (if any and if it actually changed)
        cur = await db.execute(
            "SELECT id, value FROM kg_state WHERE entity_key=? AND attribute=? "
            "AND superseded_at IS NULL", (ek, attr))
        row = await cur.fetchone()
        if row and row[1] == value:
            return  # no change — don't churn history
        if row:
            await db.execute("UPDATE kg_state SET superseded_at=? WHERE id=?", (_now(), row[0]))
        await db.execute(
            "INSERT INTO kg_state (entity_key, entity_name, attribute, value, valid_from, "
            "source, superseded_at, created_at) VALUES (?,?,?,?,?,?,NULL,?)",
            (ek, entity_name.strip(), attr, value, vf, source, _now()))
        await db.commit()


async def get_state(entity_name: str, attribute: str) -> dict | None:
    """Deterministic read of the CURRENT value. Returns None if unknown (never a guess)."""
    ek, attr = _norm(entity_name), _norm(attribute)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT entity_name, attribute, value, valid_from, source FROM kg_state "
            "WHERE entity_key=? AND attribute=? AND superseded_at IS NULL "
            "ORDER BY valid_from DESC LIMIT 1", (ek, attr))
        r = await cur.fetchone()
        return dict(r) if r else None


async def get_history(entity_name: str, attribute: str) -> list[dict]:
    """Full timeline of values for an attribute (newest first) — the episodic record."""
    ek, attr = _norm(entity_name), _norm(attribute)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT value, valid_from, source, superseded_at FROM kg_state "
            "WHERE entity_key=? AND attribute=? ORDER BY valid_from DESC, id DESC", (ek, attr))
        return [dict(r) for r in await cur.fetchall()]


async def all_current(entity_name: str) -> list[dict]:
    """All current attributes for one entity — e.g. everything known about a position."""
    ek = _norm(entity_name)
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT attribute, value, valid_from, source FROM kg_state "
            "WHERE entity_key=? AND superseded_at IS NULL ORDER BY attribute", (ek,))
        return [dict(r) for r in await cur.fetchall()]


async def state_context(entity_names: list[str]) -> str:
    """Render canonical current-state for the brain prompt. Deterministic, dated, sourced.
    This is what the read path injects INSTEAD of letting fuzzy memory answer 'current X'."""
    lines = []
    for name in entity_names:
        rows = await all_current(name)
        for r in rows:
            lines.append(f"- {name} · {r['attribute']}: {r['value']} "
                         f"(as of {r['valid_from'][:10]}, src {r['source']})")
    return ("CANONICAL CURRENT STATE (authoritative — use these over any memory):\n"
            + "\n".join(lines)) if lines else ""


# ---------------------------------------------------------------------------
# Read path: build the canonical-state block for a given incoming message.
# ---------------------------------------------------------------------------
_PORTFOLIO_KW = ("hold", "holding", "position", "portfolio", "own", "shares",
                 "sell", "sold", "buy", "stake", "long ", "short ")
_IDENTITY_KW = ("employer", "work", "role", "title", "job", "company", "where do i",
                "who am i", "live", "based", "located", "timezone")
# owner self-entity: identity attributes always worth asserting (fixes employer/role/loc class)
_OWNER_ENTITY = "Alex"


async def context_for_message(message: str, max_lines: int = 25) -> str:
    """Canonical current-state block to inject for this message. Deterministic, dated.
    Always asserts owner identity (employer/role/location); adds all holdings when the
    message is portfolio-ish; adds any entity literally named in the message."""
    msg = (message or "").lower()
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute(
            "SELECT entity_key, entity_name, attribute, value, valid_from, source "
            "FROM kg_state WHERE superseded_at IS NULL ORDER BY entity_key, attribute")
        rows = [dict(r) for r in await cur.fetchall()]
    if not rows:
        return ""
    want_portfolio = any(k in msg for k in _PORTFOLIO_KW)
    want_identity = any(k in msg for k in _IDENTITY_KW)
    keep = []
    for r in rows:
        named = r["entity_key"] in msg or r["entity_name"].lower() in msg
        is_owner = r["entity_key"] == _OWNER_ENTITY.lower()
        is_holding = r["attribute"] == "holding"
        if named or is_owner or (want_portfolio and is_holding) or (want_identity and is_owner):
            keep.append(r)
    if not keep:
        return ""
    lines = [f"- {r['entity_name']} · {r['attribute']}: {r['value']} "
             f"(as of {r['valid_from'][:10]}, src {r['source']})" for r in keep[:max_lines]]
    return ("CANONICAL CURRENT STATE — AUTHORITATIVE. Use these values for any "
            "'current' holdings/role/contact question; they OVERRIDE anything in memory or "
            "older documents. If a memory disagrees, it is stale.\n" + "\n".join(lines))


# ---------------------------------------------------------------------------
# Confirmation-gated seeding: INFERRED facts go to a pending queue, not straight
# to canonical state. Only authoritative feeders (broker/portfolio-db) or explicit user
# confirmation call set_state directly. (The lesson: never let a guess become canon.)
# ---------------------------------------------------------------------------
PENDING_SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_state_pending (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    entity_name TEXT NOT NULL, attribute TEXT NOT NULL, value TEXT,
    source TEXT, rationale TEXT, created_at TEXT NOT NULL, status TEXT DEFAULT 'pending'
);
"""

# sources trusted enough to write canon directly (hard, verifiable feeds)
AUTHORITATIVE_SOURCES = {"broker", "portfolio-db", "user", "user direct confirmation"}


async def propose_state(entity_name, attribute, value, source, rationale=""):
    """Route a fact to canon if its source is authoritative; otherwise queue it for the
    user to confirm. Returns ('applied'|'pending'). This is the anti-confabulation gate."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(PENDING_SCHEMA); await db.commit()
    if source in AUTHORITATIVE_SOURCES:
        await set_state(entity_name, attribute, value, source=source)
        return "applied"
    async with aiosqlite.connect(DB_PATH) as db:
        await db.execute(
            "INSERT INTO kg_state_pending (entity_name,attribute,value,source,rationale,created_at)"
            " VALUES (?,?,?,?,?,?)", (entity_name, attribute, value, source, rationale, _now()))
        await db.commit()
    return "pending"


async def list_pending() -> list[dict]:
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(PENDING_SCHEMA); await db.commit()
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM kg_state_pending WHERE status='pending' ORDER BY id")
        return [dict(r) for r in await cur.fetchall()]


async def confirm_pending(pending_id: int):
    """User confirms a queued fact -> it becomes canon (supersedes prior)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cur = await db.execute("SELECT * FROM kg_state_pending WHERE id=?", (pending_id,))
        r = await cur.fetchone()
        if not r:
            return False
        await set_state(r["entity_name"], r["attribute"], r["value"],
                        source=f"user-confirmed ({r['source']})")
        await db.execute("UPDATE kg_state_pending SET status='confirmed' WHERE id=?", (pending_id,))
        await db.commit()
        return True


if __name__ == "__main__":
    async def _demo():
        await init_db()
        await set_state("NBIS", "holding", "sold — 0 shares", "IBKR", "2026-05-22")
        print("get_state NBIS/holding ->", await get_state("NBIS", "holding"))
        print("history ->", await get_history("NBIS", "holding"))
    asyncio.run(_demo())
