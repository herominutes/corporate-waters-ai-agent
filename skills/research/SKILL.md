# Skill: Deep web research

## The 3-tier fetch stack
1. **HTTP** (httpx) with Readability extraction → markdown. Fast path.
2. **Stealth browser** — Patchright (CDP-patched Playwright) with stealth JS
   injected via CDP and a Cloudflare Turnstile solver. Runs as a persistent
   daemon: pages get snapshot refs (`e1`, `e2`…) so follow-up commands
   (click, fill, screenshot, console, network) address elements stably.
3. **Web Archive / cache fallback** when both fail.

Every fetch is stamped with provenance: `[fetched via http in 1.2s | 5432 chars]`.
15-minute TTL cache prevents hammering the same sources.

## Rules
- **Never answer how-to/factual questions from memory** — search first, verify
  the procedure exists, cite what was actually found. If it can't be verified,
  it doesn't go in the answer.
- **Locations/routes/distances**: always resolved via live maps, never
  estimated from district names.
- YouTube: transcript extraction via yt-dlp with authenticated browser cookies.
- Findings from web-enriched answers are auto-extracted into memory
  (`research` category) so the same question never triggers the same crawl.
