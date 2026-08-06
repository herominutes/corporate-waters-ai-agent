"""Knowledge Graph — entity-relationship store for connecting people, events, projects.

Solves the core problem: Saul can't connect "Olga cancelled" → "Olga is on event X"
→ "Heiko is also on event X" → "preserve Heiko when updating". Each piece of info
lives in isolation. The graph connects them.

Architecture:
  - SQLite tables: entities + relationships
  - Entities: people, events, projects, companies, documents
  - Relationships: attends, works_at, member_of, contacted_via, etc.
  - Auto-populates from calendar, email, Telegram, memories
  - CLI tool: `kgraph` for brain to query relationships
  - Context injection: when a message mentions a known entity,
    related entities are auto-loaded into context

Usage (CLI):
  kgraph lookup <name>              Find entity and all connections
  kgraph connections <name>         List all related entities
  kgraph add-person <name> [--email X] [--telegram X] [--phone X] [--role X] [--org X]
  kgraph add-rel <entity1> <rel_type> <entity2>
  kgraph context <text>             Extract entities from text, return full context
  kgraph sync                       Re-sync from calendar/email/memories
  kgraph dump                       Dump entire graph as readable text
"""

import asyncio
import json
import logging
import re
import sys
from datetime import datetime

import aiosqlite

from goodman.config import DB_PATH

logger = logging.getLogger(__name__)

# ------------------------------------------------------------------
# Semantic relationship types — inspired by OpenClaw's ontology.
# NOT enforced (existing data uses free-form types), but available
# as a reference and for the `suggest_relationship()` helper.
# ------------------------------------------------------------------

RELATIONSHIP_TYPES = {
    # Structural / hierarchical
    "parent_of": "Hierarchical parent (org → division, project → sub-project)",
    "member_of": "Membership in a group, org, or team",
    "example_of": "Instance or example of a category or pattern",
    "works_at": "Employment or contractor relationship",
    "manages": "Management or supervisory relationship",

    # Events / attendance
    "attends": "Attends an event, meeting, or appointment",
    "precedes": "Temporal ordering — comes before",
    "succeeds": "Temporal ordering — comes after",

    # Communication / contact
    "contacted_via": "Communication channel (email, telegram, phone)",
    "consultant_for": "Advisory or consulting relationship",

    # Knowledge / documentation
    "documents": "One entity documents or describes another",
    "summarizes": "A condensed version of another entity",
    "expands_on": "Provides more detail on another entity",
    "clarifies": "Makes another entity clearer or resolves ambiguity",
    "context_for": "Provides background context for another entity",

    # Logical / causal
    "supports": "Provides evidence or backing for another entity",
    "contradicts": "Conflicts with or disproves another entity",
    "causes": "Causal relationship — one leads to another",
    "depends_on": "Dependency — requires another entity",
    "requires": "Hard requirement or prerequisite",
    "enables": "Makes another entity possible or easier",
    "limits": "Constrains or restricts another entity",
    "conflicts_with": "Incompatible with another entity",

    # Evolution / versioning
    "supersedes": "Replaces or obsoletes an older entity",
    "corrects": "Fixes an error in another entity",
    "extends": "Adds new capability or scope to another entity",
    "refines": "Improves precision or quality of another entity",
    "augments": "Adds supplementary information or capability",
    "validates": "Confirms or verifies another entity",

    # Implementation
    "implements": "Concrete realization of a spec, plan, or design",
    "specifies": "Defines requirements or interface for another entity",

    # General
    "related_to": "Generic relationship (use when no specific type fits)",
    "originated_from": "Source or origin of an entity",
}

# Entity-type-based suggestions for relationship types
_ENTITY_SUGGESTIONS = {
    ("person", "company"): ["works_at", "member_of", "consultant_for", "manages"],
    ("person", "event"): ["attends", "manages"],
    ("person", "person"): ["manages", "related_to", "contacted_via", "consultant_for"],
    ("person", "project"): ["works_at", "manages", "member_of", "consultant_for"],
    ("person", "document"): ["documents", "originated_from"],
    ("project", "project"): ["parent_of", "depends_on", "extends", "supersedes", "related_to"],
    ("project", "company"): ["member_of", "originated_from"],
    ("document", "document"): ["supersedes", "summarizes", "expands_on", "corrects", "extends"],
    ("document", "project"): ["documents", "specifies", "context_for"],
    ("event", "event"): ["precedes", "succeeds", "related_to", "conflicts_with"],
    ("event", "project"): ["related_to", "context_for"],
    ("company", "company"): ["parent_of", "related_to", "conflicts_with"],
}


def suggest_relationship(source_type: str, target_type: str) -> list[str]:
    """Suggest appropriate relationship types based on entity types.

    Returns a list of suggested rel_type strings, most common first.
    Falls back to a generic set if the entity-type pair is unknown.
    """
    key = (source_type.lower(), target_type.lower())
    if key in _ENTITY_SUGGESTIONS:
        return list(_ENTITY_SUGGESTIONS[key])

    # Try reversed pair
    rev_key = (target_type.lower(), source_type.lower())
    if rev_key in _ENTITY_SUGGESTIONS:
        return list(_ENTITY_SUGGESTIONS[rev_key])

    # Generic fallback
    return ["related_to", "supports", "depends_on", "documents", "context_for"]


SCHEMA = """
CREATE TABLE IF NOT EXISTS kg_entities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    entity_type TEXT NOT NULL,  -- person, event, project, company, document
    properties TEXT DEFAULT '{}',  -- JSON blob for flexible attributes
    created_at TEXT DEFAULT (datetime('now')),
    updated_at TEXT DEFAULT (datetime('now')),
    UNIQUE(name, entity_type)
);

CREATE TABLE IF NOT EXISTS kg_aliases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    alias TEXT NOT NULL,
    entity_id INTEGER NOT NULL REFERENCES kg_entities(id),
    UNIQUE(alias)
);

CREATE TABLE IF NOT EXISTS kg_relationships (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id INTEGER NOT NULL REFERENCES kg_entities(id),
    rel_type TEXT NOT NULL,  -- attends, works_at, member_of, emailed, manages, etc.
    target_id INTEGER NOT NULL REFERENCES kg_entities(id),
    properties TEXT DEFAULT '{}',
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source_id, rel_type, target_id)
);

CREATE INDEX IF NOT EXISTS idx_kg_entities_name ON kg_entities(name);
CREATE INDEX IF NOT EXISTS idx_kg_entities_type ON kg_entities(entity_type);
CREATE INDEX IF NOT EXISTS idx_kg_aliases_alias ON kg_aliases(alias);
CREATE INDEX IF NOT EXISTS idx_kg_rel_source ON kg_relationships(source_id);
CREATE INDEX IF NOT EXISTS idx_kg_rel_target ON kg_relationships(target_id);
CREATE INDEX IF NOT EXISTS idx_kg_rel_type ON kg_relationships(rel_type);
"""


async def init_db():
    """Create knowledge graph tables."""
    async with aiosqlite.connect(DB_PATH) as db:
        await db.executescript(SCHEMA)
        await db.commit()
    logger.info("Knowledge graph tables initialized")


# --- Entity CRUD ---

async def upsert_entity(name: str, entity_type: str, properties: dict = None) -> int:
    """Create or update an entity. Returns entity ID."""
    props = json.dumps(properties or {})
    async with aiosqlite.connect(DB_PATH) as db:
        # Try insert
        try:
            cursor = await db.execute(
                "INSERT INTO kg_entities (name, entity_type, properties) VALUES (?, ?, ?)",
                (name, entity_type, props),
            )
            await db.commit()
            return cursor.lastrowid
        except aiosqlite.IntegrityError:
            # Update existing
            if properties:
                # Merge properties
                cursor = await db.execute(
                    "SELECT id, properties FROM kg_entities WHERE name = ? AND entity_type = ?",
                    (name, entity_type),
                )
                row = await cursor.fetchone()
                if row:
                    existing = json.loads(row[1] or "{}")
                    existing.update(properties)
                    await db.execute(
                        "UPDATE kg_entities SET properties = ?, updated_at = datetime('now') WHERE id = ?",
                        (json.dumps(existing), row[0]),
                    )
                    await db.commit()
                    return row[0]
            cursor = await db.execute(
                "SELECT id FROM kg_entities WHERE name = ? AND entity_type = ?",
                (name, entity_type),
            )
            row = await cursor.fetchone()
            return row[0] if row else 0


async def add_alias(entity_name: str, alias: str):
    """Add an alias for an entity. 'JD' → 'John Doe', 'Heiko' → 'Heiko Strubel'."""
    entities = await find_entity(entity_name)
    if not entities:
        return
    entity_id = entities[0]["id"]
    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO kg_aliases (alias, entity_id) VALUES (?, ?)",
                (alias.lower(), entity_id),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            pass  # Alias already exists


async def resolve_alias(name: str) -> int | None:
    """Resolve a name or alias to an entity ID."""
    async with aiosqlite.connect(DB_PATH) as db:
        # Try direct entity match first
        cursor = await db.execute(
            "SELECT id FROM kg_entities WHERE LOWER(name) = LOWER(?)", (name,),
        )
        row = await cursor.fetchone()
        if row:
            return row[0]
        # Try alias
        cursor = await db.execute(
            "SELECT entity_id FROM kg_aliases WHERE alias = ?", (name.lower(),),
        )
        row = await cursor.fetchone()
        return row[0] if row else None


async def find_entity(name: str) -> list[dict]:
    """Find entities by name (case-insensitive partial match)."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT id, name, entity_type, properties FROM kg_entities "
            "WHERE LOWER(name) LIKE LOWER(?) ORDER BY entity_type, name",
            (f"%{name}%",),
        )
        rows = await cursor.fetchall()
        return [
            {"id": r["id"], "name": r["name"], "type": r["entity_type"],
             "properties": json.loads(r["properties"] or "{}")}
            for r in rows
        ]


# --- Relationship CRUD ---

async def add_relationship(source_name: str, rel_type: str, target_name: str,
                           source_type: str = "person", target_type: str = "person",
                           properties: dict = None):
    """Add a relationship between two entities (creates entities if needed)."""
    source_id = await upsert_entity(source_name, source_type)
    target_id = await upsert_entity(target_name, target_type)
    props = json.dumps(properties or {})

    async with aiosqlite.connect(DB_PATH) as db:
        try:
            await db.execute(
                "INSERT INTO kg_relationships (source_id, rel_type, target_id, properties) "
                "VALUES (?, ?, ?, ?)",
                (source_id, rel_type, target_id, props),
            )
            await db.commit()
        except aiosqlite.IntegrityError:
            pass  # Relationship already exists


async def get_connections(name: str) -> list[dict]:
    """Get all entities connected to a given entity name."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Find entity IDs matching this name
        cursor = await db.execute(
            "SELECT id, name, entity_type, properties FROM kg_entities "
            "WHERE LOWER(name) LIKE LOWER(?)",
            (f"%{name}%",),
        )
        entities = await cursor.fetchall()
        if not entities:
            return []

        results = []
        for entity in entities:
            eid = entity["id"]
            props = json.loads(entity["properties"] or "{}")

            # Outgoing relationships
            cursor = await db.execute(
                "SELECT r.rel_type, e.name, e.entity_type, e.properties "
                "FROM kg_relationships r JOIN kg_entities e ON r.target_id = e.id "
                "WHERE r.source_id = ?",
                (eid,),
            )
            outgoing = [
                {"rel": r["rel_type"], "name": r["name"], "type": r["entity_type"],
                 "properties": json.loads(r["properties"] or "{}")}
                for r in await cursor.fetchall()
            ]

            # Incoming relationships
            cursor = await db.execute(
                "SELECT r.rel_type, e.name, e.entity_type, e.properties "
                "FROM kg_relationships r JOIN kg_entities e ON r.source_id = e.id "
                "WHERE r.target_id = ?",
                (eid,),
            )
            incoming = [
                {"rel": r["rel_type"], "name": r["name"], "type": r["entity_type"],
                 "properties": json.loads(r["properties"] or "{}")}
                for r in await cursor.fetchall()
            ]

            results.append({
                "entity": entity["name"],
                "type": entity["entity_type"],
                "properties": props,
                "connections": outgoing + [
                    {"rel": f"<-{r['rel']}", "name": r["name"], "type": r["type"],
                     "properties": r["properties"]}
                    for r in incoming
                ],
            })

        return results


async def get_event_attendees(event_name: str) -> list[dict]:
    """Get all attendees of an event."""
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row
        cursor = await db.execute(
            "SELECT e.name, e.properties FROM kg_entities e "
            "JOIN kg_relationships r ON r.source_id = e.id "
            "JOIN kg_entities ev ON r.target_id = ev.id "
            "WHERE ev.entity_type = 'event' AND LOWER(ev.name) LIKE LOWER(?) "
            "AND r.rel_type = 'attends'",
            (f"%{event_name}%",),
        )
        rows = await cursor.fetchall()
        return [{"name": r["name"], "properties": json.loads(r["properties"] or "{}")}
                for r in rows]


# --- Context extraction ---

async def extract_context(text: str) -> str:
    """Given a text, find all mentioned entities and return their full graph context.

    This is the key function — called before every brain call to enrich
    the prompt with relationship data. Uses alias resolution so 'Heiko',
    'Heiko Strubel', '@heikostrubel' all resolve to the same entity.
    """
    async with aiosqlite.connect(DB_PATH) as db:
        db.row_factory = aiosqlite.Row

        # Get all entity names + aliases
        cursor = await db.execute(
            "SELECT id, name, entity_type FROM kg_entities WHERE entity_type = 'person'"
        )
        all_entities = await cursor.fetchall()

        cursor = await db.execute(
            "SELECT alias, entity_id FROM kg_aliases"
        )
        aliases = {r["alias"]: r["entity_id"] for r in await cursor.fetchall()}

    # Find which entities are mentioned in the text
    text_lower = text.lower()
    mentioned_ids = set()
    mentioned = []

    for entity in all_entities:
        eid = entity["id"]
        name = entity["name"]
        if eid in mentioned_ids:
            continue
        # Match full name
        if name.lower() in text_lower:
            mentioned.append(entity)
            mentioned_ids.add(eid)
        # Match first name (for people, min 4 chars to avoid false positives)
        elif " " in name and name.split()[0].lower() in text_lower and len(name.split()[0]) > 3:
            mentioned.append(entity)
            mentioned_ids.add(eid)

    # Check aliases
    for alias, entity_id in aliases.items():
        if entity_id not in mentioned_ids and alias in text_lower:
            # Fetch the entity
            async with aiosqlite.connect(DB_PATH) as db:
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    "SELECT id, name, entity_type FROM kg_entities WHERE id = ?", (entity_id,)
                )
                entity = await cursor.fetchone()
                if entity:
                    mentioned.append(entity)
                    mentioned_ids.add(entity_id)

    if not mentioned:
        return ""

    # Get connections for each mentioned entity
    context_parts = []
    seen_ids = set()
    for entity in mentioned:
        if entity["id"] in seen_ids:
            continue
        seen_ids.add(entity["id"])
        connections = await get_connections(entity["name"])
        for conn in connections:
            lines = [f"{conn['entity']} ({conn['type']})"]
            props = conn.get("properties", {})
            if props:
                for k, v in props.items():
                    lines.append(f"  {k}: {v}")
            for c in conn.get("connections", []):
                c_props = c.get("properties", {})
                prop_str = ""
                if c_props:
                    prop_str = " — " + ", ".join(f"{k}: {v}" for k, v in c_props.items())
                lines.append(f"  {c['rel']} → {c['name']} ({c['type']}){prop_str}")
            context_parts.append("\n".join(lines))

    if not context_parts:
        return ""

    return "=== KNOWLEDGE GRAPH (entities mentioned in this message) ===\n" + \
           "\n\n".join(context_parts)


# --- Sync from existing data sources ---

async def auto_generate_aliases(entity_id: int, name: str, properties: dict):
    """Auto-generate aliases from entity name and properties.

    'Heiko Strubel' → aliases: 'heiko', 'heiko strubel', '@heikostrubel'
    """
    aliases_to_add = set()

    # Full name lowercase
    aliases_to_add.add(name.lower())

    # First name (if multi-word and long enough)
    parts = name.split()
    if len(parts) > 1 and len(parts[0]) > 3:
        aliases_to_add.add(parts[0].lower())

    # Last name
    if len(parts) > 1 and len(parts[-1]) > 3:
        aliases_to_add.add(parts[-1].lower())

    # Telegram handle
    tg = properties.get("telegram", "")
    if tg:
        aliases_to_add.add(tg.lower())
        aliases_to_add.add(tg.lower().lstrip("@"))

    # Email prefix
    email = properties.get("email", "")
    if email and "@" in email:
        prefix = email.split("@")[0].lower()
        if len(prefix) > 3:
            aliases_to_add.add(prefix)

    async with aiosqlite.connect(DB_PATH) as db:
        for alias in aliases_to_add:
            if not alias or len(alias) < 3:
                continue
            try:
                await db.execute(
                    "INSERT INTO kg_aliases (alias, entity_id) VALUES (?, ?)",
                    (alias, entity_id),
                )
            except aiosqlite.IntegrityError:
                pass
        await db.commit()


async def sync_from_contacts_notebook():
    """Parse contacts.md notebook and populate the graph."""
    from goodman.notebooks import read_notebook

    content = read_notebook("contacts")
    if not content:
        return 0

    count = 0
    current_org = ""

    for line in content.split("\n"):
        line = line.strip()
        if line.startswith("## "):
            current_org = line[3:].strip()
            continue
        if not line.startswith("- "):
            continue

        # Parse: "- Name | email | @telegram | role"
        parts = [p.strip() for p in line[2:].split("|")]
        if not parts:
            continue

        name = parts[0]
        props = {}

        for part in parts[1:]:
            part = part.strip()
            if "@" in part and "." in part and not part.startswith("@"):
                props["email"] = part
            elif part.startswith("@"):
                props["telegram"] = part
            elif part.startswith("+"):
                props["phone"] = part
            else:
                props["role"] = part

        if current_org:
            props["org"] = current_org

        eid = await upsert_entity(name, "person", props)
        await auto_generate_aliases(eid, name, props)

        # Add org relationship
        if current_org and current_org not in ("Boss", "RULE"):
            await upsert_entity(current_org, "company")
            await add_relationship(name, "member_of", current_org,
                                   source_type="person", target_type="company")

        count += 1

    return count


async def sync_from_calendar():
    """Sync calendar events into the graph with attendee relationships."""
    try:
        from goodman import calendar
        events_text = await asyncio.to_thread(calendar.get_upcoming_events, 14)
        if not events_text:
            return 0

        # Parse events — format varies, but typically "Title | Date | Attendees"
        count = 0
        for line in events_text.split("\n"):
            if not line.strip() or line.startswith("=") or line.startswith("-"):
                continue
            # Extract event name and any emails
            emails = re.findall(r'[\w.-]+@[\w.-]+\.\w+', line)
            # Extract title (usually first part before date/time)
            title = line.split("|")[0].strip() if "|" in line else line[:60].strip()

            if title and len(title) > 3:
                event_id = await upsert_entity(title, "event")
                for email in emails:
                    # Find person with this email or create
                    name = email.split("@")[0].replace(".", " ").title()
                    person_id = await upsert_entity(name, "person", {"email": email})
                    await add_relationship(name, "attends", title,
                                           source_type="person", target_type="event")
                count += 1
        return count
    except Exception as e:
        logger.error("Calendar sync failed: %s", e)
        return 0


async def full_sync():
    """Run all sync sources."""
    await init_db()
    contacts = await sync_from_contacts_notebook()
    cal_events = await sync_from_calendar()
    return {"contacts": contacts, "calendar_events": cal_events}


# --- CLI ---

def _format_connections(connections: list[dict]) -> str:
    """Format connection data for CLI output."""
    if not connections:
        return "No connections found."

    lines = []
    for conn in connections:
        lines.append(f"\n{conn['entity']} ({conn['type']})")
        props = conn.get("properties", {})
        for k, v in props.items():
            lines.append(f"  {k}: {v}")
        for c in conn.get("connections", []):
            c_props = c.get("properties", {})
            prop_str = ""
            if c_props:
                prop_str = " — " + ", ".join(f"{k}: {v}" for k, v in c_props.items())
            lines.append(f"  {c['rel']} → {c['name']} ({c['type']}){prop_str}")
    return "\n".join(lines)


async def _cli_async():
    args = sys.argv[1:]
    if not args:
        print("Usage: kgraph lookup|connections|add-person|add-rel|context|sync|dump")
        return

    await init_db()
    cmd = args[0].lower()

    if cmd == "lookup" and len(args) > 1:
        name = " ".join(args[1:])
        entities = await find_entity(name)
        if entities:
            for e in entities:
                print(f"{e['name']} ({e['type']})")
                for k, v in e["properties"].items():
                    print(f"  {k}: {v}")
        else:
            print(f"No entity matching '{name}'")

    elif cmd == "connections" and len(args) > 1:
        name = " ".join(args[1:])
        connections = await get_connections(name)
        print(_format_connections(connections))

    elif cmd == "add-person" and len(args) > 1:
        name = args[1]
        props = {}
        i = 2
        while i < len(args):
            if args[i] == "--email" and i + 1 < len(args):
                props["email"] = args[i + 1]; i += 2
            elif args[i] == "--telegram" and i + 1 < len(args):
                props["telegram"] = args[i + 1]; i += 2
            elif args[i] == "--phone" and i + 1 < len(args):
                props["phone"] = args[i + 1]; i += 2
            elif args[i] == "--role" and i + 1 < len(args):
                props["role"] = args[i + 1]; i += 2
            elif args[i] == "--org" and i + 1 < len(args):
                props["org"] = args[i + 1]; i += 2
            else:
                i += 1
        eid = await upsert_entity(name, "person", props)
        print(f"Entity '{name}' created/updated (id={eid})")

    elif cmd == "add-rel" and len(args) >= 4:
        source, rel_type, target = args[1], args[2], " ".join(args[3:])
        await add_relationship(source, rel_type, target)
        print(f"Relationship added: {source} --{rel_type}--> {target}")

    elif cmd == "context" and len(args) > 1:
        text = " ".join(args[1:])
        ctx = await extract_context(text)
        print(ctx if ctx else "No entities found in text.")

    elif cmd == "attendees" and len(args) > 1:
        event = " ".join(args[1:])
        attendees = await get_event_attendees(event)
        if attendees:
            for a in attendees:
                props = a["properties"]
                email = props.get("email", "no email")
                print(f"  {a['name']} ({email})")
        else:
            print(f"No attendees found for event matching '{event}'")

    elif cmd == "sync":
        result = await full_sync()
        print(f"Synced: {result['contacts']} contacts, {result['calendar_events']} calendar events")

    elif cmd == "rel-types":
        print("=== Available Relationship Types ===")
        for rel, desc in RELATIONSHIP_TYPES.items():
            print(f"  {rel:20s} {desc}")
        if len(args) > 1:
            # Optional: suggest types for a source→target pair
            # Usage: kgraph rel-types person company
            if len(args) >= 3:
                suggestions = suggest_relationship(args[1], args[2])
                print(f"\nSuggested for {args[1]} → {args[2]}: {', '.join(suggestions)}")

    elif cmd == "dump":
        async with aiosqlite.connect(DB_PATH) as db:
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT id, name, entity_type, properties FROM kg_entities ORDER BY entity_type, name"
            )
            entities = await cursor.fetchall()
            print(f"=== {len(entities)} entities ===")
            for e in entities:
                props = json.loads(e["properties"] or "{}")
                prop_str = ", ".join(f"{k}={v}" for k, v in props.items()) if props else ""
                print(f"  [{e['entity_type']}] {e['name']}" + (f" ({prop_str})" if prop_str else ""))

            cursor = await db.execute(
                "SELECT s.name as source, r.rel_type, t.name as target "
                "FROM kg_relationships r "
                "JOIN kg_entities s ON r.source_id = s.id "
                "JOIN kg_entities t ON r.target_id = t.id "
                "ORDER BY s.name"
            )
            rels = await cursor.fetchall()
            print(f"\n=== {len(rels)} relationships ===")
            for r in rels:
                print(f"  {r['source']} --{r['rel_type']}--> {r['target']}")

    else:
        print("Usage: kgraph lookup|connections|add-person|add-rel|context|attendees|rel-types|sync|dump")


def _cli():
    asyncio.run(_cli_async())


if __name__ == "__main__":
    _cli()
