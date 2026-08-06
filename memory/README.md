# Memory — everything Saul knows

The memory system is the moat: a 78k-row SQLite database plus append-only
transcripts, built so that nothing said once is ever truly lost.

## Folders
| Folder | Contents |
|---|---|
| `MEMORY.md` | The always-loaded index — compact, curated, injected into every prompt |
| `daily/` | Daily activity logs the agent appends to as it works |
| `transcripts/` | Every conversation, one markdown file per day — **never compacted**; searchable via BM25 so context survives even after the live session is trimmed |
| `transcripts-archive/` | Older months |
| `knowledge-graph/` | The whole memory rendered as an interactive force-graph (see below) |
| `schema.md` | Live database schema with row counts |

## Write path
1. The model emits `[REMEMBER: category | fact]` directives in its answers.
2. Web-research findings are auto-extracted after enriched responses.
3. **Dedup & supersession**: a new memory with ≥0.93 cosine similarity to an
   existing one replaces it; same-topic corrections (detected by verdict words
   + ≥0.78 similarity in the same category) supersede stale facts — so "I sold
   NVDA" retires "holds NVDA" instead of coexisting with it.

## Read path
Hybrid retrieval per message: BM25 keyword scoring + embedding cosine, with a
recency boost for identity/preference categories. Transcript search runs in
parallel so old conversations are recallable verbatim.

## Knowledge graph
`knowledge-graph/` contains the exporter and viewer: 9.5k memories + 85
entities as a WebGL force-graph — nodes colored by category, MECE layers
(Product / Contacts / Personal), per-PM network filters, and a calibrated
**context-coverage metric** (0–100%) that scores how much industry / business
/ product / user knowledge the graph actually contains, probe-by-probe. The
live instance runs behind basic auth on the VPS.
