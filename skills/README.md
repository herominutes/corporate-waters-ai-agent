# Skills — procedural memory

Skills are *how Saul does recurring jobs* — distinct from soul (identity) and
memory (facts). Two provenances:

- **Hand-built** (`scheduling/`, `recruiting/`, `research/`,
  `automations/`) — distilled from real incidents and
  iterated with the boss. The hard parts are enforced as code gates in
  `tools/`, the judgment parts live here as playbooks.
- **Auto-generated** (`hermes/`) — Saul writes his own skills: after a
  successful multi-step session, a generator distills the procedure into a
  markdown skill with frontmatter. At runtime the top-3 skills relevant to the
  incoming message (embedding similarity) are injected into context.
  Benchmarked against a no-skill baseline (benchmark code not included in this snapshot).

| Group | Skill |
|---|---|
| `scheduling/` | Multi-account calendar operations with safety gates |
| `recruiting/` | ATS pipeline discipline + candidate sourcing |
| `research/` | Deep web research with a 3-tier fetch stack |
| `automations/` | Recurring jobs Saul runs unattended |
| `hermes/` | 6 self-generated skills (financial analysis, hiring evaluation, immigration case-building…) |
