# The Goodman Protocol — setup & porting guide

Personal executive-assistant chatbot (Telegram + Slack) with a Saul Goodman
persona. This guide covers standing it up on a **fresh VPS**.

## What is NOT in this repo (you must provision it on the new box)
Everything sensitive/personal is gitignored and does **not** travel with the code:
- `.env` — all tokens/keys (copy `.env.example` → `.env` and fill in)
- `credentials/` — Google OAuth client (`google-oauth.json`), service account
  (`service-account.json`), and per-account tokens (`oauth-tokens/*.json`)
- `data/` — runtime state: SQLite DBs, transcripts, browser profile/cookies, uploads
- `data/soul/` — the persona + memory (`SOUL.md`, `MEMORY.md`, `USER.md`, `SPEC.md`, `memory/`)
- `wgcf-account.toml` / `wireproxy.conf` — WARP/proxy creds (optional)

Bring these over **out of band** (scp/rsync from the old box), never through git.

## Prerequisites (system)
- **Python 3.11+**
- **ffmpeg** — audio transcription (faster-whisper) + yt-dlp
- **Chromium/Chrome** + Playwright/Patchright browser deps (browser automation)
- The **`claude` CLI** — the brain shells out to it (`brain.py`). Install Claude Code and
  authenticate it for the service user; the bot calls `claude --print ...`.

```bash
sudo apt update && sudo apt install -y python3 python3-pip python3-venv ffmpeg
```

## Install
```bash
git clone <this-repo> goodman && cd goodman
python3 -m pip install -r requirements.txt
python3 -m playwright install chromium          # + patchright browser
python3 -m playwright install-deps              # system libs for headless chromium
cp .env.example .env                            # then fill in every key
```

## Credentials / auth
1. Put your Google files in `credentials/`:
   `google-oauth.json` (OAuth client), `service-account.json`, and any existing
   `oauth-tokens/*.json`. If tokens are missing/expired: `python3 reauth.py <account>`.
2. Telethon: first run will create a session from `TELETHON_API_ID/HASH`.
3. Slack: install the app, set bot/app/user tokens + `SLACK_OWNER_USER_ID`.
4. (Optional) `~/bin/claude` authenticated; `caddy` for the HTTPS API (`start.sh`).

## Authorization model (important)
Non-owner Slack callers are gated in code (see `project_saul_privileged_action_gates`):
no impersonation, calendar deletes limited to PA-booked events, email domain allowlist,
and per-user capability tiers via `SLACK_USER_TIERS` / `SLACK_DEFAULT_TIER`. Configure
these in `.env` before exposing the bot to a team.

## Run
```bash
bash run.sh          # supervisor: auto-restarts on crash (the production way)
# or, with the HTTPS API + Caddy:
bash start.sh
```
`run.sh` launches `python3 -m goodman.bot` with `PYTHONPATH=src` and backs off on crash.
Verify in `logs/bot.log`: "open for business" + `getUpdates 200`.

## Porting notes
- The git remote uses an SSH host alias (`github-goodman`) — set up the matching entry in
  `~/.ssh/config` + deploy key on the new box, or just re-point `origin` to a normal
  `git@github.com:...` URL.
- Run **one** bot instance per Telegram token (two pollers = 409 conflict).
- The droplet is RAM-sensitive; the bot leaks over long uptimes — `run.sh` mitigates by
  respawning. Give it ≥2 GB and consider swap.
