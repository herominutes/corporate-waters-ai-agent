"""SQLite persistence layer for The Goodman Protocol."""

import asyncio
import logging
import math
import re
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

import aiosqlite
from goodman.config import DB_PATH, TELEGRAM_ALLOWED_USER_ID

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Connection pool — reuse connections instead of open/close per call
# ---------------------------------------------------------------------------
_pool: asyncio.Queue | None = None
# Must comfortably exceed the per-brain-call DB fan-out (~9 concurrent reads in
# brain.think's gather). At 4 the pool self-starved every turn; losers hit the
# 10s acquire timeout and silently returned empty context (the amnesia bug).
_POOL_SIZE = 10


async def _init_pool():
    """Create the connection pool (called once from init_db)."""
    global _pool
    if _pool is not None:
        return
    _pool = asyncio.Queue(maxsize=_POOL_SIZE)
    for _ in range(_POOL_SIZE):
        conn = await aiosqlite.connect(DB_PATH)
        await _apply_pragmas(conn)
        conn.row_factory = aiosqlite.Row
        await _pool.put(conn)
    log.info("Connection pool initialized (%d connections)", _POOL_SIZE)


async def _apply_pragmas(conn):
    """Apply performance PRAGMAs to a connection."""
    await conn.execute("PRAGMA journal_mode=WAL")
    await conn.execute("PRAGMA synchronous=NORMAL")
    await conn.execute("PRAGMA cache_size=-8000")  # 8MB
    await conn.execute("PRAGMA mmap_size=67108864")  # 64MB
    await conn.execute("PRAGMA temp_store=MEMORY")


class _PoolConn:
    """Async context manager that borrows a connection from the pool."""
    def __init__(self):
        self._conn = None

    async def __aenter__(self):
        if _pool is None:
            # Fallback before pool is initialized
            self._conn = await aiosqlite.connect(DB_PATH)
            self._conn.row_factory = aiosqlite.Row
            self._fallback = True
            return self._conn
        try:
            self._conn = await asyncio.wait_for(_pool.get(), timeout=10.0)
            self._fallback = False
            return self._conn
        except asyncio.TimeoutError:
            # Pool exhausted under load. Fall back to a direct connection rather
            # than raising — a raised TimeoutError used to be swallowed upstream
            # into empty context (Saul answering blind). Loud + degrades safely.
            log.warning(
                "DB pool acquire timed out (size=%d) — using direct fallback connection",
                _POOL_SIZE,
            )
            self._conn = await aiosqlite.connect(DB_PATH)
            await _apply_pragmas(self._conn)
            self._conn.row_factory = aiosqlite.Row
            self._fallback = True
            return self._conn

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        if self._fallback:
            await self._conn.close()
        else:
            if exc_type is not None:
                try:
                    await self._conn.rollback()
                except Exception:
                    pass
            if _pool is not None:
                await _pool.put(self._conn)
        self._conn = None


def get_conn():
    """Get a pooled connection context manager."""
    return _PoolConn()


def _get_boss_user_id() -> str:
    return TELEGRAM_ALLOWED_USER_ID

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS goals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT,
    progress INTEGER DEFAULT 0,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'completed', 'abandoned')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS context (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS conversation_summaries (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    summary TEXT NOT NULL,
    msg_range_start INTEGER NOT NULL,
    msg_range_end INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_conversations_user_created
    ON conversations(user_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_memories_user
    ON memories(user_id);
CREATE INDEX IF NOT EXISTS idx_summaries_user
    ON conversation_summaries(user_id, created_at DESC);

CREATE TABLE IF NOT EXISTS scheduled_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    schedule_type TEXT NOT NULL CHECK(schedule_type IN ('once', 'every', 'daily', 'weekly')),
    schedule_data TEXT NOT NULL DEFAULT '{}',
    payload TEXT NOT NULL,
    enabled INTEGER DEFAULT 1,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    next_run_at TEXT NOT NULL,
    last_run_at TEXT,
    run_count INTEGER DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_jobs_next_run
    ON scheduled_jobs(next_run_at);

CREATE TABLE IF NOT EXISTS tasks (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    description TEXT NOT NULL,
    status TEXT DEFAULT 'active' CHECK(status IN ('active', 'blocked', 'completed', 'abandoned')),
    priority TEXT DEFAULT 'normal' CHECK(priority IN ('high', 'normal', 'low')),
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_attempt_at TIMESTAMP,
    failure_reason TEXT,
    attempts_count INTEGER DEFAULT 0,
    next_approach TEXT,
    context TEXT DEFAULT '{}'
);

CREATE INDEX IF NOT EXISTS idx_tasks_user_status
    ON tasks(user_id, status);
"""


async def init_db():
    """Initialize the database schema."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Apply WAL mode + PRAGMAs before schema creation
        await _apply_pragmas(db)

        await db.executescript(SCHEMA)

        # Migration: add embedding column if missing
        cursor = await db.execute("PRAGMA table_info(memories)")
        cols = {row[1] for row in await cursor.fetchall()}
        if "embedding" not in cols:
            await db.execute("ALTER TABLE memories ADD COLUMN embedding BLOB")
            log.info("Added embedding column to memories table.")

        # Migration: add context_from column for job chaining
        cursor = await db.execute("PRAGMA table_info(scheduled_jobs)")
        job_cols = {row[1] for row in await cursor.fetchall()}
        if "context_from" not in job_cols:
            await db.execute("ALTER TABLE scheduled_jobs ADD COLUMN context_from INTEGER")
            log.info("Added context_from column to scheduled_jobs table.")
        if "last_output" not in job_cols:
            await db.execute("ALTER TABLE scheduled_jobs ADD COLUMN last_output TEXT")
            log.info("Added last_output column to scheduled_jobs table.")

        # Migration: create FTS5 index if missing
        try:
            cursor = await db.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='conversations_fts'"
            )
            if not await cursor.fetchone():
                log.info("Creating FTS5 index for conversations...")
                await db.execute("""
                    CREATE VIRTUAL TABLE conversations_fts USING fts5(
                        content, content='conversations', content_rowid='id'
                    )
                """)
                await db.execute("""
                    CREATE TRIGGER conversations_ai AFTER INSERT ON conversations BEGIN
                        INSERT INTO conversations_fts(rowid, content) VALUES (new.id, new.content);
                    END
                """)
                await db.execute("""
                    CREATE TRIGGER conversations_ad AFTER DELETE ON conversations BEGIN
                        INSERT INTO conversations_fts(conversations_fts, rowid, content)
                        VALUES('delete', old.id, old.content);
                    END
                """)
                await db.execute("""
                    CREATE TRIGGER conversations_au AFTER UPDATE ON conversations BEGIN
                        INSERT INTO conversations_fts(conversations_fts, rowid, content)
                        VALUES('delete', old.id, old.content);
                        INSERT INTO conversations_fts(rowid, content) VALUES (new.id, new.content);
                    END
                """)
                # Backfill existing conversations
                await db.execute(
                    "INSERT INTO conversations_fts(rowid, content) "
                    "SELECT id, content FROM conversations"
                )
                await db.commit()
                log.info("FTS5 index created and backfilled.")
        except Exception as e:
            log.warning("FTS5 setup failed (may not be supported): %s", e)

        await db.commit()

    # Initialize connection pool (after schema is ready)
    await _init_pool()

    # Backfill any memories missing embeddings
    await _backfill_embeddings()


async def _backfill_embeddings():
    """One-time backfill: embed any memories with NULL embedding."""
    async with get_conn() as db:
        cursor = await db.execute(
            "SELECT id, content FROM memories WHERE embedding IS NULL"
        )
        rows = await cursor.fetchall()
        if not rows:
            return

        log.info("Backfilling embeddings for %d memories...", len(rows))
        from goodman.embeddings import embed_batch, vec_to_bytes

        texts = [row[1] for row in rows]
        vectors = embed_batch(texts)
        for (row_id, _), vec in zip(rows, vectors):
            await db.execute(
                "UPDATE memories SET embedding = ? WHERE id = ?",
                (vec_to_bytes(vec), row_id),
            )
        await db.commit()
        log.info("Backfill complete: %d memories embedded.", len(rows))
    _invalidate_mem_cache()


async def save_message(user_id: str, role: str, content: str):
    """Save a conversation message."""
    async with get_conn() as db:
        await db.execute(
            "INSERT INTO conversations (user_id, role, content) VALUES (?, ?, ?)",
            (user_id, role, content),
        )
        await db.commit()


async def get_recent_messages(
    user_id: str, limit: int = 30, exclude_summarized: bool = True
) -> list[dict]:
    """Get recent conversation messages for context.

    By default excludes messages already covered by a saved compaction
    summary — summaries are injected separately, so returning the raw rows
    too double-injects the same content into the prompt.
    """
    async with get_conn() as db:
        db.row_factory = aiosqlite.Row
        if exclude_summarized:
            cursor = await db.execute(
                "SELECT id, role, content, created_at FROM conversations "
                "WHERE user_id = ? AND id > ("
                "SELECT COALESCE(MAX(msg_range_end), 0) "
                "FROM conversation_summaries WHERE user_id = ?"
                ") ORDER BY created_at DESC LIMIT ?",
                (user_id, user_id, limit),
            )
        else:
            cursor = await db.execute(
                "SELECT id, role, content, created_at FROM conversations "
                "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
                (user_id, limit),
            )
        rows = await cursor.fetchall()
        return [
            {"id": row["id"], "role": row["role"], "content": row["content"],
             "created_at": row["created_at"]}
            for row in reversed(rows)
        ]


async def get_message_count(user_id: str) -> int:
    """Get total message count for a user."""
    async with get_conn() as db:
        cursor = await db.execute(
            "SELECT COUNT(*) FROM conversations WHERE user_id = ?", (user_id,)
        )
        row = await cursor.fetchone()
        return row[0]


async def get_unsummarized_messages(user_id: str, before_id: int, limit: int = 40) -> list[dict]:
    """Get older messages that haven't been summarized yet."""
    async with get_conn() as db:
        db.row_factory = aiosqlite.Row
        # Find the highest summarized message ID
        cursor = await db.execute(
            "SELECT COALESCE(MAX(msg_range_end), 0) FROM conversation_summaries WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        last_summarized = row[0]

        cursor = await db.execute(
            "SELECT id, role, content, created_at FROM conversations "
            "WHERE user_id = ? AND id > ? AND id < ? ORDER BY created_at ASC LIMIT ?",
            (user_id, last_summarized, before_id, limit),
        )
        rows = await cursor.fetchall()
        return [
            {"id": row["id"], "role": row["role"], "content": row["content"],
             "created_at": row["created_at"]}
            for row in rows
        ]


# --- Memories (permanent facts) ---

async def save_memory(user_id: str, category: str, content: str):
    """Save a permanent memory/fact about the user."""
    from goodman.embeddings import embed, vec_to_bytes

    async with get_conn() as db:
        # Avoid exact duplicates
        cursor = await db.execute(
            "SELECT id FROM memories WHERE user_id = ? AND content = ?",
            (user_id, content),
        )
        if await cursor.fetchone():
            return
        vec = vec_to_bytes(embed(content))
        await db.execute(
            "INSERT INTO memories (user_id, category, content, embedding) VALUES (?, ?, ?, ?)",
            (user_id, category, content, vec),
        )
        await db.commit()
    _invalidate_mem_cache()


async def get_memories(user_id: str) -> list[dict]:
    """Get all permanent memories about the user, plus shared memories from the boss."""
    async with get_conn() as db:
        db.row_factory = aiosqlite.Row
        boss_id = _get_boss_user_id()
        if boss_id and user_id != boss_id:
            cursor = await db.execute(
                "SELECT category, content FROM memories "
                "WHERE user_id = ? OR (user_id = ? AND visibility = 'shared') "
                "ORDER BY created_at ASC",
                (user_id, boss_id),
            )
        else:
            cursor = await db.execute(
                "SELECT category, content FROM memories WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            )
        rows = await cursor.fetchall()
        return [{"category": row["category"], "content": row["content"]} for row in rows]


# Loading ~77k rows (each with a 1.5KB embedding blob) from SQLite on EVERY
# message was ~100MB+ of transient allocations per call — a major RSS-churn
# hot path. Cache the loaded list per user; invalidate on any memory write.
_MEM_CACHE_TTL = 300.0
_mem_cache: dict[str, tuple[float, list[dict]]] = {}


def _invalidate_mem_cache():
    _mem_cache.clear()


async def _get_memories_with_embeddings(user_id: str) -> list[dict]:
    """Get all memories including their embedding vectors and created_at.

    For non-boss users, also includes shared memories from the boss.
    Returned list is shared via cache — callers must NOT mutate it.
    """
    cached = _mem_cache.get(user_id)
    if cached and time.time() - cached[0] < _MEM_CACHE_TTL:
        return cached[1]
    async with get_conn() as db:
        db.row_factory = aiosqlite.Row
        boss_id = _get_boss_user_id()
        if boss_id and user_id != boss_id:
            cursor = await db.execute(
                "SELECT id, user_id, category, content, embedding, created_at FROM memories "
                "WHERE user_id = ? OR (user_id = ? AND visibility = 'shared') "
                "ORDER BY created_at ASC",
                (user_id, boss_id),
            )
        else:
            cursor = await db.execute(
                "SELECT id, user_id, category, content, embedding, created_at FROM memories "
                "WHERE user_id = ? ORDER BY created_at ASC",
                (user_id,),
            )
        rows = await cursor.fetchall()
        result = [
            {"id": row["id"], "user_id": row["user_id"],
             "category": row["category"], "content": row["content"],
             "embedding": row["embedding"], "created_at": row["created_at"]}
            for row in rows
        ]
    _mem_cache[user_id] = (time.time(), result)
    return result


async def get_relevant_memories(
    user_id: str, query: str, top_k: int = 15
) -> list[dict]:
    """Get memories ranked by hybrid BM25 + semantic similarity scoring.

    Blends keyword matching (BM25) with cosine similarity from sentence
    embeddings for better recall on semantically related memories.

    Falls back to get_memories() if query is empty or too short.
    """
    all_memories = await _get_memories_with_embeddings(user_id)
    # Exclude task_state memories — they are injected separately via get_active_task_states()
    all_memories = [m for m in all_memories if m.get("category") != "task_state"]
    if len(all_memories) <= top_k:
        return [_strip_embedding(m) for m in all_memories]
    if not query or len(query.strip()) < 3:
        # No usable query: return the freshest personal memories, NOT the whole
        # table (the old behavior dumped ALL rows oldest-first and let the char
        # cap keep arbitrary ancient ones).
        personal = [m for m in all_memories if m.get("category") != "document"]
        personal.sort(key=lambda m: m.get("created_at") or "", reverse=True)
        return [_strip_embedding(m) for m in personal[:top_k]]

    scored = _score_memories_hybrid(all_memories, query, top_k)
    return scored


def _tokenize(text: str) -> list[str]:
    """Simple word tokenizer — lowercase, strip punctuation."""
    return re.findall(r'[a-zA-Z0-9]+', text.lower())


def _strip_embedding(mem: dict) -> dict:
    """Return a memory dict without the embedding field."""
    return {"category": mem["category"], "content": mem["content"]}


def _bm25_scores(memories: list[dict], query_tokens: list[str]) -> list[float]:
    """Compute BM25-style scores for each memory against query tokens."""
    doc_freq: Counter = Counter()
    memory_tokens: list[list[str]] = []
    for mem in memories:
        tokens = _tokenize(mem["content"])
        memory_tokens.append(tokens)
        for t in set(tokens):
            doc_freq[t] += 1

    n_docs = len(memories)
    scores = []
    for i, mem in enumerate(memories):
        tokens = memory_tokens[i]
        if not tokens:
            scores.append(0.0)
            continue
        tf = Counter(tokens)
        score = 0.0
        for qt in query_tokens:
            if qt in tf:
                term_freq = tf[qt] / len(tokens)
                idf = (n_docs - doc_freq.get(qt, 0) + 0.5) / (doc_freq.get(qt, 0) + 0.5)
                if idf > 0:
                    score += term_freq * math.log(1 + idf)
        scores.append(score)
    return scores


def _semantic_scores(memories: list[dict], query: str) -> list[float]:
    """Compute cosine similarity between query and each memory embedding."""
    import numpy as np
    from goodman.embeddings import embed

    # np.frombuffer is a zero-copy view; bytes_to_vec(...).tolist() blew each
    # 1.5KB blob into ~12KB of Python floats × 70k+ rows per call → RSS ratchet
    query_vec = np.asarray(embed(query), dtype=np.float32)
    scores = []
    for mem in memories:
        emb = mem.get("embedding")
        if emb:
            scores.append(float(np.dot(query_vec, np.frombuffer(emb, dtype=np.float32))))
        else:
            scores.append(0.0)
    return scores


def _normalize(scores: list[float]) -> list[float]:
    """Min-max normalize a list of scores to [0, 1]."""
    lo = min(scores) if scores else 0.0
    hi = max(scores) if scores else 0.0
    if hi - lo < 1e-9:
        return [0.0] * len(scores)
    return [(s - lo) / (hi - lo) for s in scores]


def _score_memories_hybrid(
    memories: list[dict], query: str, top_k: int
) -> list[dict]:
    """Hybrid BM25 + semantic scoring with identity-memory guarantee.

    Separates personal memories from document chunks to prevent
    book/file content from drowning out personal facts.
    """
    query_tokens = _tokenize(query)
    if not query_tokens:
        return [_strip_embedding(m) for m in memories[:top_k]]

    raw_sem = _semantic_scores(memories, query)
    bm25 = _normalize(_bm25_scores(memories, query_tokens))
    semantic = _normalize(raw_sem)

    # Relevance gate: a memory earns a slot only if it has genuine signal —
    # raw cosine >= 0.25 (MiniLM: unrelated text sits ~0.0-0.2) or a keyword
    # score >= 35% of the best keyword match. Without this, top_k was ALWAYS
    # filled and pure filler rode along on every single call.
    relevant = [rs >= 0.25 or nb >= 0.35 for rs, nb in zip(raw_sem, bm25)]

    # Blend: 40% BM25 + 60% semantic
    blended = [0.4 * b + 0.6 * s for b, s in zip(bm25, semantic)]

    # Temporal decay: score *= exp(-days_old / 30)
    # Identity and preference memories are permanent — no decay applied, but
    # they get a small recency TIEBREAK (+0.05 max, 90d half-life-ish) so a
    # newer correction outranks a stale near-equal on the same topic
    # (Rififi incident: a May taste memory beat four June corrections).
    now = datetime.now(timezone.utc)
    for i, mem in enumerate(memories):
        created_at_str = mem.get("created_at")
        cat = mem.get("category", "")
        if not created_at_str:
            continue
        try:
            created_at = datetime.fromisoformat(created_at_str.replace("Z", "+00:00"))
            if created_at.tzinfo is None:
                created_at = created_at.replace(tzinfo=timezone.utc)
            days_old = (now - created_at).total_seconds() / 86400.0
            if cat not in ("identity", "preference", "document", "research"):
                blended[i] *= math.exp(-days_old / 30.0)
            elif cat in ("identity", "preference"):
                blended[i] += 0.05 * math.exp(-days_old / 90.0)
        except (ValueError, TypeError):
            pass  # Skip decay if timestamp is malformed

    # Category boost/penalty
    # Transcripts (.md / .txt files) are short conversational sources — boost them.
    # Book/YouTube/large-PDF chunks (everything else under 'document') stay penalized.
    transcript_re = re.compile(r'^\[file:[^\]]*\.(md|txt)\]', re.IGNORECASE)
    for i, mem in enumerate(memories):
        cat = mem.get("category", "")
        if cat in ("identity", "preference"):
            blended[i] += 0.3
        elif cat in ("work", "legal", "finance"):
            blended[i] += 0.1
        elif cat == "document":
            if transcript_re.match(mem["content"]):
                blended[i] += 0.25  # Surface meeting/discovery transcripts
            else:
                blended[i] -= 0.15  # Deprioritize book/YouTube chunks

    scored = sorted(enumerate(memories), key=lambda x: -blended[x[0]])

    # Always include the top-scoring identity memories first (capped — there are
    # ~80 identity rows; without a cap they'd flood the result and crowd out everything else).
    identity_cap = 4
    result = []
    added_contents: set[str] = set()
    identity_scored = sorted(
        (i for i, m in enumerate(memories) if m.get("category") == "identity"),
        key=lambda i: -blended[i],
    )
    for i in identity_scored[:identity_cap]:
        mem = memories[i]
        if mem["content"] not in added_contents:
            result.append(_strip_embedding(mem))
            added_contents.add(mem["content"])

    # Partition into transcript / personal / book-doc buckets.
    # - transcript_cap: dedicated slots for .md/.txt transcripts so they survive
    #   competition with high-boost identity rows.
    # - per_file_cap: prevents one chatty source from filling all transcript slots.
    transcript_cap = 4
    book_cap = 4
    per_file_cap = 2
    personal_cap = top_k - transcript_cap - book_cap
    personal_count = 0
    transcript_count = 0
    book_count = 0
    per_file: Counter = Counter()

    file_re = re.compile(r'^\[(file|youtube):([^\]]+)\]')
    for i, mem in scored:
        if len(result) >= top_k:
            break
        if not relevant[i]:
            continue
        if mem["content"] in added_contents:
            continue
        if mem.get("category") == "identity":
            continue  # already capped above
        is_doc = mem.get("category") == "document"
        if is_doc:
            is_transcript = bool(transcript_re.match(mem["content"]))
            m = file_re.match(mem["content"])
            file_key = m.group(2) if m else "_"
            if per_file[file_key] >= per_file_cap:
                continue
            if is_transcript:
                if transcript_count >= transcript_cap:
                    continue
                transcript_count += 1
            else:
                if book_count >= book_cap:
                    continue
                book_count += 1
            per_file[file_key] += 1
        else:
            if personal_count >= personal_cap:
                continue
            personal_count += 1
        result.append(_strip_embedding(mem))
        added_contents.add(mem["content"])

    # If we still have space (e.g. fewer than personal_cap personal), fill — but
    # only with RELEVANT rows; never leak more identity rows past the cap, and
    # never leak past-cap books. Returning fewer than top_k is fine: filler
    # memories are noise, not context.
    for i, mem in scored:
        if len(result) >= top_k:
            break
        if not relevant[i]:
            continue
        if mem.get("category") == "identity":
            continue
        if mem["content"] not in added_contents:
            result.append(_strip_embedding(mem))
            added_contents.add(mem["content"])

    return result


async def save_memory_fuzzy(user_id: str, category: str, content: str):
    """Save a memory, checking for fuzzy duplicates first.

    Catches cases where the same fact is stated in different words:
    e.g., "User's name is Alex" vs "The boss is called Alex"

    Also handles SUPERSEDING memories: if the new memory contradicts or
    updates an old one about the same subject, the old one is replaced
    instead of both being kept. This prevents memory bloat where 50+
    entries accumulate on the same topic with conflicting information.
    """
    import numpy as np
    from goodman.embeddings import embed

    existing = await _get_memories_with_embeddings(user_id)

    content_tokens = set(_tokenize(content))
    if not content_tokens:
        return

    content_lower = content.lower()
    new_vec_np = np.asarray(embed(content), dtype=np.float32)

    # Collect memories on the same topic. Two detectors:
    #   - token overlap (cheap, catches verbatim-ish restatements)
    #   - embedding cosine (catches paraphrases token overlap misses —
    #     the Rififi incident: 4 paraphrased corrections coexisted)
    same_topic_mems = []

    for mem in existing:
        # Exact match — skip entirely
        if mem["content"] == content:
            return

        cos = 0.0
        if mem.get("embedding"):
            try:
                cos = float(np.dot(new_vec_np, np.frombuffer(mem["embedding"], dtype=np.float32)))
            except Exception:
                cos = 0.0

        if cos >= 0.93:
            # Semantic near-duplicate, skip saving
            return

        mem_tokens = set(_tokenize(mem["content"]))
        if not mem_tokens:
            continue

        overlap = len(content_tokens & mem_tokens)
        smaller = min(len(content_tokens), len(mem_tokens))
        if smaller == 0:
            continue

        ratio = overlap / smaller

        if ratio > 0.7:
            # >70% overlap = near-duplicate, skip saving
            return

        if ratio > 0.4 or (cos >= 0.78 and mem.get("category") == category):
            # Same topic, different content — where contradictions live
            same_topic_mems.append(mem)

    # If we found memories on the same topic, check if this is an UPDATE
    # (new info supersedes old) vs genuinely new info.
    # Heuristic: if the new memory has negation/status-change/verdict language
    # about a subject mentioned in old memories, replace the old ones.
    supersede_signals = [
        "permanently off", "do not", "don't", "never recommend", "never use",
        "never again", "stopped", "removed", "cancelled", "dropped", "bad for",
        "causes", "paradoxical", "backfired", "failed", "doesn't work",
        "не работает", "contraindicated", "avoid", "banned", "off the list",
        "is bad", "submitted", "completed", "done", "resolved", "finished",
        "paid", "changed to", "updated to", "moved to", "switched to",
        "now using", "hated", "hates", "disliked", "dislikes", "bailed",
        "was a flop", "total flop", "worst", "ненавидит", "не понравил",
        "бросил", "провал",
    ]

    is_superseding = any(signal in content_lower for signal in supersede_signals)

    if is_superseding and same_topic_mems:
        # Replace old same-topic memories with the new one.
        # user_id guard: shared boss memories surfaced for other users have a
        # different user_id, so the DELETE can never touch them.
        async with get_conn() as conn:
            for old_mem in same_topic_mems:
                await conn.execute(
                    "DELETE FROM memories WHERE id = ? AND user_id = ?",
                    (old_mem["id"], user_id),
                )
                log.info("Superseded memory #%d: %s", old_mem["id"], old_mem["content"][:80])
            await conn.commit()
        _invalidate_mem_cache()

    # Save the new memory
    await save_memory(user_id, category, content)


async def update_memory(user_id: str, memory_id: int, new_content: str):
    """Update an existing memory's content by ID."""
    async with get_conn() as conn:
        await conn.execute(
            "UPDATE memories SET content = ? WHERE id = ? AND user_id = ?",
            (new_content, memory_id, user_id),
        )
        await conn.commit()
    _invalidate_mem_cache()


async def delete_memory(user_id: str, content: str):
    """Delete a specific memory."""
    async with get_conn() as db:
        await db.execute(
            "DELETE FROM memories WHERE user_id = ? AND content = ?",
            (user_id, content),
        )
        await db.commit()
    _invalidate_mem_cache()


# --- Conversation summaries ---

async def get_max_summary_end(user_id: str) -> int:
    """Highest message ID already covered by a saved summary (0 if none)."""
    async with get_conn() as db:
        cursor = await db.execute(
            "SELECT COALESCE(MAX(msg_range_end), 0) FROM conversation_summaries "
            "WHERE user_id = ?",
            (user_id,),
        )
        row = await cursor.fetchone()
        return row[0]


async def save_summary(user_id: str, summary: str, msg_start: int, msg_end: int):
    """Save a conversation summary for a range of messages."""
    async with get_conn() as db:
        await db.execute(
            "INSERT INTO conversation_summaries (user_id, summary, msg_range_start, msg_range_end) "
            "VALUES (?, ?, ?, ?)",
            (user_id, summary, msg_start, msg_end),
        )
        await db.commit()


async def get_summaries(user_id: str, limit: int = 5) -> list[str]:
    """Get the most recent conversation summaries."""
    async with get_conn() as db:
        cursor = await db.execute(
            "SELECT summary FROM conversation_summaries "
            "WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        )
        rows = await cursor.fetchall()
        return [row[0] for row in reversed(rows)]


# --- Context key-value store ---

async def set_context(key: str, value: str):
    """Store a key-value context item."""
    async with get_conn() as db:
        await db.execute(
            "INSERT INTO context (key, value, updated_at) VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(key) DO UPDATE SET value = ?, updated_at = CURRENT_TIMESTAMP",
            (key, value, value),
        )
        await db.commit()


async def get_context(key: str) -> str | None:
    """Retrieve a context value."""
    async with get_conn() as db:
        cursor = await db.execute("SELECT value FROM context WHERE key = ?", (key,))
        row = await cursor.fetchone()
        return row[0] if row else None


# --- Scheduled jobs ---

async def create_job(name: str, schedule_type: str, schedule_data: str,
                     payload: str, next_run_at: str) -> int:
    """Create a new scheduled job. Returns the job ID."""
    async with get_conn() as conn:
        cursor = await conn.execute(
            "INSERT INTO scheduled_jobs (name, schedule_type, schedule_data, payload, next_run_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (name, schedule_type, schedule_data, payload, next_run_at),
        )
        await conn.commit()
        return cursor.lastrowid


async def get_due_jobs(now_iso: str) -> list[dict]:
    """Get all enabled jobs whose next_run_at is <= now."""
    async with get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT id, name, schedule_type, schedule_data, payload, next_run_at, run_count "
            "FROM scheduled_jobs WHERE enabled = 1 AND next_run_at <= ?",
            (now_iso,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def update_job_run(job_id: int, last_run_at: str, next_run_at: str):
    """Update a job after execution."""
    async with get_conn() as conn:
        await conn.execute(
            "UPDATE scheduled_jobs SET last_run_at = ?, next_run_at = ?, "
            "run_count = run_count + 1 WHERE id = ?",
            (last_run_at, next_run_at, job_id),
        )
        await conn.commit()


async def delete_job(job_id: int):
    """Delete a scheduled job."""
    async with get_conn() as conn:
        await conn.execute("DELETE FROM scheduled_jobs WHERE id = ?", (job_id,))
        await conn.commit()


async def save_job_output(job_id: int, output: str):
    """Save the output of a job run for potential chaining."""
    async with get_conn() as conn:
        # Truncate output to prevent bloat
        truncated = output[:5000] if len(output) > 5000 else output
        await conn.execute(
            "UPDATE scheduled_jobs SET last_output = ? WHERE id = ?",
            (truncated, job_id),
        )
        await conn.commit()


async def get_chained_context(job_id: int) -> str | None:
    """Get the output from a job's context_from source job."""
    async with get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT context_from FROM scheduled_jobs WHERE id = ?", (job_id,)
        )
        row = await cursor.fetchone()
        if not row or not row["context_from"]:
            return None

        cursor = await conn.execute(
            "SELECT last_output, name FROM scheduled_jobs WHERE id = ?",
            (row["context_from"],)
        )
        source = await cursor.fetchone()
        if source and source["last_output"]:
            return f"[Output from job '{source['name']}']\n{source['last_output']}"
        return None


async def get_recent_cross_platform_messages(prefix: str, limit: int = 20) -> list[dict]:
    """Get recent messages from cross-platform conversations.

    Args:
        prefix: user_id prefix to match, e.g. "tg_" or "wa_"
        limit: max messages to return
    Returns:
        List of dicts with user_id, role, content, created_at
    """
    async with get_conn() as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT user_id, role, content, created_at FROM conversations "
            "WHERE user_id LIKE ? ORDER BY created_at DESC LIMIT ?",
            (f"{prefix}%", limit),
        )
        rows = await cursor.fetchall()
        return [
            {"user_id": row["user_id"], "role": row["role"],
             "content": row["content"], "created_at": row["created_at"]}
            for row in reversed(rows)
        ]


async def get_cross_platform_messages_since(since: str, limit: int = 50) -> list[dict]:
    """Get incoming cross-platform DMs (TG DMs, WhatsApp, Slack) since a timestamp.

    Only returns role='user' messages (incoming), not bot responses. Telegram
    GROUP/channel chatter (user_id like 'tg_group_%') is EXCLUDED — the boss does
    not want digest compilations of group channels, only real direct messages.
    """
    async with get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT user_id, content, created_at FROM conversations "
            "WHERE ((user_id LIKE 'tg_%' AND user_id NOT LIKE 'tg_group_%') "
            "       OR user_id LIKE 'wa_%' OR user_id LIKE 'slack_%') "
            "AND role = 'user' AND created_at > ? "
            "ORDER BY created_at ASC LIMIT ?",
            (since, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def search_conversations_fts(
    user_id: str, query: str, limit: int = 20, days: int | None = None
) -> list[dict]:
    """Full-text search across conversation history using FTS5.

    Falls back to LIKE if FTS5 is unavailable. Returns results ranked by relevance.
    """
    terms = [t.strip() for t in query.lower().split() if t.strip()]
    if not terms:
        return []

    async with get_conn() as conn:
        conn.row_factory = aiosqlite.Row

        try:
            fts_query = " AND ".join(f'"{t}"' for t in terms)

            # Step 1: pure FTS5 match — fast, no JOIN
            cursor = await conn.execute(
                "SELECT rowid, bm25(conversations_fts) AS rank "
                "FROM conversations_fts WHERE conversations_fts MATCH ? "
                "ORDER BY rank LIMIT ?",
                (fts_query, limit * 3),
            )
            fts_rows = await cursor.fetchall()
            if not fts_rows:
                return []

            # Step 2: hydrate + filter by user_id/date from conversations table
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
            return [{"role": row["role"], "content": row["content"],
                     "created_at": row["created_at"]} for row in rows]
        except Exception:
            pass

        # Fallback: LIKE
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

        cursor = await conn.execute(
            f"SELECT role, content, created_at FROM conversations "
            f"WHERE {' AND '.join(conditions)} ORDER BY created_at DESC LIMIT ?",
            params_like,
        )
        rows = await cursor.fetchall()
        return [{"role": row["role"], "content": row["content"],
                 "created_at": row["created_at"]} for row in rows]


async def list_jobs() -> list[dict]:
    """List all enabled scheduled jobs."""
    async with get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            "SELECT id, name, schedule_type, payload, next_run_at, run_count "
            "FROM scheduled_jobs WHERE enabled = 1 ORDER BY next_run_at ASC"
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


# --- Task queue ---

async def create_task(user_id: str, description: str, priority: str = "normal",
                      context: str = "{}") -> int | None:
    """Create a persistent task. Returns task_id, or None if duplicate."""
    # Fuzzy dedup against open tasks
    existing = await get_all_open_tasks(user_id)
    desc_tokens = set(_tokenize(description))
    if desc_tokens:
        for t in existing:
            t_tokens = set(_tokenize(t["description"]))
            if not t_tokens:
                continue
            overlap = len(desc_tokens & t_tokens)
            smaller = min(len(desc_tokens), len(t_tokens))
            if smaller > 0 and overlap / smaller > 0.7:
                log.debug("Task dedup: '%s' matches existing #%d", description[:50], t["id"])
                return None

    async with get_conn() as conn:
        cursor = await conn.execute(
            "INSERT INTO tasks (user_id, description, priority, context) "
            "VALUES (?, ?, ?, ?)",
            (user_id, description, priority, context),
        )
        await conn.commit()
        log.info("Created task #%d: %s", cursor.lastrowid, description[:80])
        return cursor.lastrowid


async def update_task(task_id: int, **kwargs):
    """Generic task update. Accepts status, failure_reason, next_approach, etc."""
    allowed = {"status", "failure_reason", "next_approach", "attempts_count",
               "last_attempt_at", "context", "priority"}
    fields = {k: v for k, v in kwargs.items() if k in allowed}
    if not fields:
        return
    fields["updated_at"] = "CURRENT_TIMESTAMP"

    set_parts = []
    values = []
    for k, v in fields.items():
        if v == "CURRENT_TIMESTAMP":
            set_parts.append(f"{k} = CURRENT_TIMESTAMP")
        else:
            set_parts.append(f"{k} = ?")
            values.append(v)
    values.append(task_id)

    async with get_conn() as conn:
        await conn.execute(
            f"UPDATE tasks SET {', '.join(set_parts)} WHERE id = ?",
            values,
        )
        await conn.commit()


async def get_tasks_by_status(user_id: str, status: str, limit: int = 20) -> list[dict]:
    """Get tasks filtered by status, ordered by priority then age."""
    priority_order = "CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 WHEN 'low' THEN 3 END"
    async with get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"SELECT * FROM tasks WHERE user_id = ? AND status = ? "
            f"ORDER BY {priority_order}, created_at ASC LIMIT ?",
            (user_id, status, limit),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def get_active_tasks(user_id: str) -> list[dict]:
    return await get_tasks_by_status(user_id, "active")


async def get_blocked_tasks(user_id: str) -> list[dict]:
    return await get_tasks_by_status(user_id, "blocked")


async def get_all_open_tasks(user_id: str) -> list[dict]:
    """Get all active + blocked tasks."""
    priority_order = "CASE priority WHEN 'high' THEN 1 WHEN 'normal' THEN 2 WHEN 'low' THEN 3 END"
    async with get_conn() as conn:
        conn.row_factory = aiosqlite.Row
        cursor = await conn.execute(
            f"SELECT * FROM tasks WHERE user_id = ? AND status IN ('active', 'blocked') "
            f"ORDER BY {priority_order}, created_at ASC LIMIT 50",
            (user_id,),
        )
        rows = await cursor.fetchall()
        return [dict(row) for row in rows]


async def complete_task(task_id: int):
    await update_task(task_id, status="completed")
    log.info("Completed task #%d", task_id)


async def abandon_task(task_id: int, reason: str = ""):
    await update_task(task_id, status="abandoned", failure_reason=reason)
    log.info("Abandoned task #%d: %s", task_id, reason[:80])


async def mark_task_blocked(task_id: int, failure_reason: str, next_approach: str = ""):
    """Mark task as blocked, increment attempts, record failure and next approach."""
    async with get_conn() as conn:
        await conn.execute(
            "UPDATE tasks SET status = 'blocked', failure_reason = ?, next_approach = ?, "
            "attempts_count = attempts_count + 1, last_attempt_at = CURRENT_TIMESTAMP, "
            "updated_at = CURRENT_TIMESTAMP WHERE id = ?",
            (failure_reason, next_approach, task_id),
        )
        await conn.commit()
    log.info("Blocked task #%d: %s", task_id, failure_reason[:80])


async def find_task_by_description(user_id: str, query: str) -> dict | None:
    """Fuzzy-match a task by description against open tasks."""
    tasks = await get_all_open_tasks(user_id)
    query_tokens = set(_tokenize(query))
    if not query_tokens:
        return None

    best_match = None
    best_score = 0.0

    for t in tasks:
        t_tokens = set(_tokenize(t["description"]))
        if not t_tokens:
            continue
        overlap = len(query_tokens & t_tokens)
        smaller = min(len(query_tokens), len(t_tokens))
        if smaller > 0:
            score = overlap / smaller
            if score > best_score and score > 0.4:
                best_score = score
                best_match = t

    return best_match
