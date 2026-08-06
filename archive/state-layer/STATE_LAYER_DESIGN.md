# Canonical State Layer — design

Status: in progress (2026-06-28). Owner: Alex. Goal: kill the recurring class of
"Saul asserts a fact that used to be true" errors (held XYZ after it was sold; stale
stale employer; wrong contact email) — across ALL domains, not
just finance.

## The disease (root cause)

Saul has two unreconciled stores, and mutable state is kept in the wrong one:

- **`memories` (fuzzy free-text) + ingested document chunks** do TWO incompatible jobs:
  episodic history AND current-state answers. Mutable facts are *appended* as new
  memories; the old values **never retire**, so stale values survive and win retrieval
  (`db.get_relevant_memories` BM25+semantic). Correcting Saul in chat just appends yet
  another competing memory.
- **`knowledge_graph` (structured)** — `kg_entities`(+aliases,+relationships incl
  `supersedes`/`corrects`) is the right substrate but is barely used (auto-creates people
  from email/Slack handles) and is **not** the authoritative read path for "current X".

Net: **current state is stored as append-only fuzzy episodic memory with no canonical,
recency-aware layer.** Every "what is X now" question is a similarity lottery over stale
text. This is general — positions, contacts, employer, project status all fail the same way.

## The fix — one mechanism for every domain

Separate **STATE** (mutable, has a current value) from **HISTORY** (episodic, append-only).

### 1. Canonical state lives in the KG, overwrite-on-change
New table `kg_state`: every mutable fact = an entity attribute with value + provenance +
validity. An update **closes out** the prior value (sets `superseded_at`) and inserts a new
current row — so we keep full history AND have a single current view.

```
kg_state(
  id, entity_key, attribute, value,
  valid_from, source, superseded_at NULL=current, created_at
)
```
A holding, a contact email, an employer are all just `(entity, attribute, value)`:
- `(XYZ, holding, "sold/0 shares")` valid_from 2026-05, source "the broker/manual"
- `(Alex, employer, "stealth startup")` valid_from 2026-06
- `(Andrey Pavlov, email, "a.pavlov@…")` valid_from …

### 2. Deterministic state reads — never fuzzy memory
`get_state(entity, attribute)` returns the single current row. Any "current X of Y"
question reads this FIRST and treats it as truth. Domain-agnostic — this is the general
version of "wire in the portfolio tracker," but the substrate is the KG, and the broker feed/Slack are
*feeders*, not the fix.

### 3. Episodic vs state separation
`memories` + ingested docs become **history only** — always carried/read WITH their date
("as of DATE …") and **excluded from current-state answers**. An old transcript listing
XYZ then reads as *"on 2026-06-17 a transcript listed XYZ,"* never *"you hold XYZ."*

### 4. Write path routes state-changes to upserts
The memory extractor must detect state changes ("sold XYZ", "left the old company", "her email
is now…") and call `set_state(...)` (which supersedes the prior value) instead of dropping
another free-text memory.

### 5. Feeders reconcile from ground truth
Authoritative sources periodically upsert into `kg_state` so canonical state self-heals:
the broker statement / the portfolio DB (positions), Slack/Gmail directory (contacts).

## Read contract (how the brain uses it)
1. Resolve entities mentioned in the message (existing `kg.find_entity`/aliases).
2. For any state-bearing attribute, inject the **canonical** value from `get_state`.
3. Fuzzy memories are injected as dated history, and the prompt forbids deriving current
   holdings/contacts/employer from them.

## Rollout
- P1: `state_layer` module (table + set/get/history) — DONE in this pass.
- P2: seed canonical state from authoritative sources + record the XYZ sale — DONE.
- P3: eval harness proving recall↑ / error↓ vs the current fuzzy path — DONE.
- P4 (next): wire `get_state` into `context.py` read path; route extractor writes; add feeders.

## Success metric
A labeled benchmark of "current X of Y" questions across ≥2 domains. Report **recall**
(% answered with the correct current value) and **error rate** (% answered with a stale/
wrong value) for the current fuzzy-memory path vs the canonical state path. Improvement
must show in the numbers.
