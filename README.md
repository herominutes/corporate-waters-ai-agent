<div align="center">

# 🎩 Saul — an AI Chief of Staff

### *"You don't need a chatbot. You need a guy who knows a guy."*

**A personal executive assistant built on Claude, running 24/7 on a single VPS.**
He books meetings, answers coworkers on Slack, screens candidates, does deep web research —
and remembers every conversation he has ever had.

![Python](https://img.shields.io/badge/Python-3.11-3776AB?logo=python&logoColor=white)
![Claude](https://img.shields.io/badge/Brain-Claude-D97757)
![Telegram](https://img.shields.io/badge/Chat-Telegram-26A5E4?logo=telegram&logoColor=white)
![Slack](https://img.shields.io/badge/Coworkers-Slack-4A154B?logo=slack&logoColor=white)
![SQLite](https://img.shields.io/badge/Memory-78k_rows-003B57?logo=sqlite&logoColor=white)
![Uptime](https://img.shields.io/badge/Runs-24%2F7-2ea44f)

</div>

---

## What is this?

Saul is not a demo, a weekend project, or a framework. He is a **production personal agent** that runs continuously, handling the real calendar, real inbox, real recruiting pipeline, and real research workload of one very demanding boss.

This repo is a **structural snapshot** of the whole system — the identity files, the memory architecture, the skills, and the capability code — sanitized for public viewing. The live instance runs from this same code with credentials and databases that are (deliberately) not here.

The interesting part isn't any single feature. It's the **anatomy**: how identity, memory, skills, and tools are separated into four layers that evolve at different speeds.

```
Telegram ──► bot.py ──► brain.py ──► Claude (subprocess)
                │            │
   Slack ───────┤            ├── 🧠 soul/      who he is        (changes rarely)
   HTTP API ────┘            ├── 📚 memory/    what he knows     (changes constantly)
                             ├── 🛠  skills/    how he works      (learned from wins)
                             └── ⚙️  tools/     what he can touch (code, not prompts)
```

---

## Highlights

- 🗓 **Three Google accounts, one calendar brain** — books meetings with conflict gates, canonical contact resolution, and ICS invites for external attendees.
- 💬 **Answers coworkers on Slack as himself** — with per-user capability tiers, automatic timezone handling, and code-enforced rules about who can ask for what.
- 🧠 **A memory that never forgets — or clings to stale facts** — 78k rows, hybrid BM25 + embedding retrieval, and *supersession*: "I sold NVDA" automatically retires "holds NVDA" instead of coexisting with it.
- ✍️ **Writes his own skills** — after a successful multi-step session, a generator distills the procedure into a markdown skill; the top-3 relevant skills are injected into context per message.
- 🌐 **3-tier web research stack** — httpx + Readability → stealth browser with Turnstile solving → web-archive fallback. If a page can be read, Saul reads it.
- 🕸 **His entire memory as an interactive knowledge graph** — 9.5k memories + 85 entities in a WebGL force-graph, with a calibrated coverage metric that scores what he actually knows.
- ⏰ **Heartbeat, not just request/response** — a scheduler wakes him to triage app reviews, watch inboxes, and run recurring jobs unattended (with a cost-optimized triage cascade).
- 🔒 **Deterministic guardrails** — the rules that matter are enforced in code, not prompted. More on that below.

---

## The four layers

### 🧠 `soul/` — identity

The system prompt ([`SOUL.md`](soul/SOUL.md)) opens with a mission and a **rule-precedence hierarchy** born from real failures:

> **LAYER 1 — TRUTH.** A confident invention is the only unforgivable output.
> **LAYER 2 — THE BOSS'S INTERESTS.** Security, confidentiality, never making him look bad.
> **LAYER 3 — DO THE WORK.** No deflecting tasks back, no workarounds sold as fixes.
> **LAYER 4 — STYLE.** Concision, voice, staying in character.
>
> *Nearly every historical failure has the same shape: letting Layer 4 (sounding capable) override Layer 1 (truth).*

Every rule in the file exists because of a documented screwup. The soul is a changelog of lessons wearing a personality.

Also here: [`TOOLS.md`](soul/TOOLS.md) (how each capability must be used) and [`HEARTBEAT.md`](soul/HEARTBEAT.md) (how to behave when nobody's watching).

### 📚 `memory/` — the moat

Everything else can be rebuilt in a weekend. The memory can't.

| Mechanism | What it does |
|---|---|
| **Transcripts** | Every conversation logged to daily markdown, **never compacted** — searchable via BM25 so context survives long after the live session is trimmed |
| **`[REMEMBER:]` directives** | The model tags facts worth keeping in its own answers; a parser extracts and stores them |
| **Dedup & supersession** | ≥0.93 cosine similarity → replace; same-topic corrections → stale fact retired, not duplicated |
| **Hybrid retrieval** | BM25 keyword + embedding cosine per message, recency-boosted for identity/preference categories |
| **Knowledge graph** | The full memory as a force-graph with MECE layers and a probe-based context-coverage score (0–100%) |

See [`memory/README.md`](memory/README.md) for the full write/read paths and [`memory/schema.md`](memory/schema.md) for the live schema.

### 🛠 `skills/` — procedural memory

Skills are *how Saul does recurring jobs* — distinct from identity and facts. Two provenances:

- **Hand-built** ([`scheduling/`](skills/scheduling/SKILL.md), [`recruiting/`](skills/recruiting/SKILL.md), [`research/`](skills/research/SKILL.md), [`automations/`](skills/automations/SKILL.md)) — distilled from real incidents; the hard parts became code gates, the judgment parts became playbooks.
- **Self-generated** ([`hermes/`](skills/hermes/)) — after a successful session, Saul writes the procedure down as a skill with frontmatter. Benchmarked against a no-skill baseline before shipping.

### ⚙️ `tools/` — capabilities as code

Every domain follows the same pattern: a library module plus a CLI the agent calls via Bash — so **every capability is testable by hand and observable in logs**.

| Domain | What Saul can do with it |
|---|---|
| [`core/`](tools/core/) | Telegram handler, Claude brain subprocess, prompt assembly, SQLite memory engine, heartbeat, message debouncing, OpenAI-compatible HTTP API |
| [`google-workspace/`](tools/google-workspace/) | Calendar + Gmail across three accounts, with conflict gates and a send-domain allowlist |
| [`slack/`](tools/slack/) | Channel search, DM listener, answering coworkers with per-user capability tiers |
| [`web-research/`](tools/web-research/) | The 3-tier fetch stack + persistent stealth-browser daemon with snapshot refs |
| [`recruiting/`](tools/recruiting/) | ATS integration with a code guard that blocks cross-discipline vacancy attaches |
| [`atlassian/`](tools/atlassian/) · [`trello/`](tools/trello/) | Confluence, Jira, boards |
| [`knowledge/`](tools/knowledge/) | Book ingestion, entity extraction for the knowledge graph |
| [`automations/`](tools/automations/) | Cron jobs — e.g. daily app-review triage that translates ≤3★ reviews and tags owners in Slack |

---

## Design principles

1. **Code gates over prompt promises.** Who can be emailed, whose calendar can be touched, which candidates attach to which vacancies — enforced in tool code. The prompt asks nicely; the code says no.
2. **Truth beats fluency.** The #1 documented failure mode of a capable persona-agent is confidently inventing things to stay in character. The rule hierarchy exists to break that tie, every time.
3. **Never delete history.** Transcripts are append-only. Compaction summarizes the working context, but the raw record is always searchable. Memory can be *superseded*, never silently lost.
4. **Everything is a CLI.** If the agent can do it, a human can do it by hand from the shell — which makes every failure reproducible and every capability auditable.
5. **The soul is a changelog.** Rules don't come from imagination; they come from incidents. Each one names the failure that created it.

---

## Repo map

```
├── CLAUDE.md          Root guardrails — output rules the agent must never break
├── soul/              Identity: system prompt, tool doctrine, heartbeat behavior
├── memory/            Index, schema, knowledge-graph exporter + WebGL viewer
├── skills/            Hand-built playbooks + self-generated skills
├── tools/             Capability code, grouped by domain
├── archive/           Past experiments: db migrations, state-layer design
└── ops/               run.sh supervisor, Caddy, systemd unit, setup guide
```

## What's deliberately *not* here

Credentials, tokens, databases, browser profiles, transcripts, and anything personal — this is the skeleton, not the body. The live instance holds the data; the architecture is the part worth sharing.

---

<div align="center">

*Built by one person and one agent, iterating on each other.*

**It's showtime.** 🎬

</div>
