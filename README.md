# Saul — a personal AI chief-of-staff

Saul is a Telegram-native AI assistant built on Claude, running 24/7 on a VPS.
He manages calendars across three Google accounts, reads and sends email, books
meetings for coworkers over Slack, screens candidates in DemoATS, monitors app
reviews, does deep web research with a stealth browser, and remembers everything
in a 78k-row SQLite memory with vector embeddings.

This repo is a structural snapshot for demonstration — the live system runs from
the same code with credentials and databases that are (deliberately) not here.

## Layout

| Folder | What it is |
|---|---|
| `CLAUDE.md` | Root guardrails — hard output rules the agent must never break |
| `soul/` | Identity: system prompt (`SOUL.md`), tool instructions, heartbeat behavior, user profile, strategy docs |
| `skills/` | Procedural memory: `scheduling/`, `recruiting/`, `research/`, `automations/`, and `hermes/` — skills the agent generated for itself from successful sessions |
| `memory/` | Everything Saul remembers: `MEMORY.md` index, `daily/` logs, `transcripts/` (every conversation, never compacted), `knowledge-graph/` (the whole memory as an interactive force-graph with a coverage metric), `schema.md` |
| `tools/` | Capability code grouped by domain: `core/` engine, `google-workspace/`, `slack/`, `atlassian/`, `web-research/`, `recruiting/`, `knowledge/`, `automations/`, `trello/` |
| `archive/` | Past generations: db migrations and the state-layer design experiment |
| `ops/` | Deployment: supervisor (`run.sh`), Caddy reverse proxy, systemd unit, setup guide |

## How it fits together

```
Telegram ──► bot.py ──► brain.py ──► claude CLI (subprocess)
                │            │
   Slack ───────┤            ├── soul/SOUL.md   (who he is)
   HTTP API ────┘            ├── memory/        (what he knows — BM25 + embeddings)
                             ├── skills/        (how he does recurring jobs)
                             └── tools/*_cli.py (what he can touch)
```

Memory is the moat: every exchange is logged to a transcript, distilled into
scored memories with 384-dim embeddings, deduplicated, superseded when facts
change, and recalled per-message by hybrid BM25 + cosine retrieval.
