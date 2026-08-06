# Tools — Saul's capability layer

Every folder is a domain. Most tools follow the same pattern: a library module
(`x.py`) plus a CLI (`x_cli.py`) that the agent calls via Bash — so every
capability is testable by hand and observable in logs.

| Folder | What Saul can do with it |
|---|---|
| `core/` | The engine: Telegram handler (`bot.py`), Claude brain subprocess (`brain.py`), system-prompt assembly (`soul.py`), SQLite memory + hybrid BM25/embedding retrieval (`db.py`), heartbeat scheduler, message steering/debounce, HTTP API (OpenAI-compatible), skills engine |
| `google-workspace/` | Three Google accounts (personal, work, PA): calendar booking with conflict + contact gates, Gmail read/send with domain allowlist, Google Docs, canonical contact resolution |
| `slack/` | Reads/searches channels, answers coworkers as Saul with per-user capability tiers, listens for DMs, books meetings for teammates (auto timezone: Almaty for callers, Vilnius for the boss) |
| `atlassian/` | Confluence pages + Jira (REST v3, same credentials) — candidate scans, epic audits |
| `web-research/` | 3-tier fetching (httpx+Readability → stealth Patchright browser with Turnstile solver → web-archive fallback), persistent browser daemon with snapshot refs, LinkedIn parser, flight search, consulate slot monitor |
| `recruiting/` | DemoATS ATS (with a code guard that blocks cross-discipline vacancy attaches) |
| `knowledge/` | Book library ingestion, entity/relationship extraction (knowledge graph tables) |
| `automations/` | Cron jobs — e.g. daily mobile-review triage that translates ≤3★ reviews and tags charter owners in Slack |

Design principle: **deterministic code gates over prompt promises.** Rules that
matter (who can be emailed, which vacancies a candidate can be attached to,
whose calendar can be touched) are enforced in the tool code, not just written
in the system prompt.
