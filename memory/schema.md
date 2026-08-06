# Memory database schema (live counts, 2026-07-15)

```sql
-- conversations: 86,141 rows
CREATE TABLE conversations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('user', 'assistant')),
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- kg_aliases: 119 rows
CREATE TABLE kg_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias TEXT NOT NULL,
    entity_id INTEGER NOT NULL REFERENCES kg_entities(id),
    UNIQUE(alias)
);

-- kg_entities: 85 rows
CREATE TABLE kg_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,  -- person, event, project, company, document
    properties TEXT DEFAULT '{}',  -- JSON blob for flexible attributes
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(name, entity_type)
);

-- kg_relationships: 41 rows
CREATE TABLE kg_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES kg_entities(id),
    rel_type TEXT NOT NULL,  -- attends, works_at, member_of, emailed, manages, etc.
    target_id INTEGER NOT NULL REFERENCES kg_entities(id),
    properties TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source_id, rel_type, target_id)
);

-- memories: 78,268 rows
CREATE TABLE memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id TEXT NOT NULL,
    category TEXT NOT NULL,
    content TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
, embedding BLOB, visibility TEXT NOT NULL DEFAULT 'personal');

```

Embeddings: 384-dim float32 (MiniLM), stored as BLOB, normalized — cosine = dot product.