"""Heartbeat - periodic proactive check-ins. The agent doesn't wait, it acts."""

import asyncio
import hashlib
import json
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from goodman import config
from goodman.config import DATA_DIR, TELEGRAM_ALLOWED_USER_ID, TIMEZONE

logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_MINUTES = 60
ACTIVE_HOURS_START = 8  # 8 AM
ACTIVE_HOURS_END = 23  # 11 PM
HEARTBEAT_OK = "HEARTBEAT_OK"

# Similarity threshold for dedup — if >70% of words overlap, it's a duplicate
_DEDUP_SIMILARITY_THRESHOLD = 0.70


def _dead_oauth_accounts() -> list[tuple[str, "object"]]:
    """Return [(email, token_path)] for OAuth tokens whose refresh actually fails.

    Tests each token live instead of guessing the account from an error string.
    The old substring guess mislabeled a dead PA token (assistant@acme-corp.example)
    as the WORK account because it contains 'acme' — so the boss was told to
    re-auth the one healthy token daily while the real dead ones stayed dead.
    """
    import json as _json
    from google.oauth2.credentials import Credentials
    from google.auth.transport.requests import Request
    from goodman import calendar as _cal

    dead = []
    for tf in _cal._find_all_token_files():
        try:
            d = _json.loads(tf.read_text())
        except Exception:
            continue
        email = d.get("email") or tf.stem
        creds = Credentials(
            token=d.get("access_token"), refresh_token=d.get("refresh_token"),
            token_uri=d.get("token_uri"), client_id=d.get("client_id"),
            client_secret=d.get("client_secret"), scopes=_cal.SCOPES)
        try:
            creds.refresh(Request())
        except Exception:
            dead.append((email, tf))
    return dead


class Heartbeat:
    """Periodic proactive check-in system — event-driven, quiet by default.

    Reads HEARTBEAT.md for tasks, gathers data (email, calendar, Slack),
    asks the brain if anything genuinely needs the boss's attention,
    and delivers proactive messages via Telegram. No scheduled digests.

    Dedup + cooldown + daily-cap state persists to disk so restarts
    don't cause repeat pings.
    """

    def __init__(
        self,
        send_callback,
        interval_minutes: int = DEFAULT_INTERVAL_MINUTES,
    ):
        """
        Args:
            send_callback: async callable(text: str) that sends a message
                           to the user via Telegram.
            interval_minutes: how often to run heartbeat checks.
        """
        self.send = send_callback
        self.interval = interval_minutes * 60
        self.running = False
        self._task = None
        self._consecutive_errors = 0
        self._beat_count = 0
        # Dedup: track recently sent message hashes to prevent duplicates
        self._sent_hashes: list[str] = []  # Last N message hashes
        self._max_sent_hashes = 10
        # Cooldown: after sending a proactive message, stay quiet for this
        # many seconds before sending another. Prevents spam.
        self._cooldown_seconds = 120 * 60  # 2 hours
        self._last_sent_time: float = 0  # wall-clock (time.time) of last send
        # Daily cap: max proactive messages per day (imminent-meeting
        # warnings are exempt from cap AND cooldown).
        self._max_sends_per_day = 2
        self._sends_today = 0
        self._sends_date: str | None = None
        self._state_path = DATA_DIR / "heartbeat_state.json"
        self._load_state()

    # ------------------------------------------------------------------
    # Persistent state (survives bot restarts)
    # ------------------------------------------------------------------

    def _load_state(self):
        try:
            data = json.loads(self._state_path.read_text())
            self._sent_hashes = data.get("sent_hashes", [])[-self._max_sent_hashes:]
            self._last_sent_time = float(data.get("last_sent_time", 0))
            self._sends_today = int(data.get("sends_today", 0))
            self._sends_date = data.get("sends_date")
        except FileNotFoundError:
            pass
        except Exception as e:
            logger.warning("Heartbeat: failed to load state (%s) — starting fresh", e)

    def _save_state(self):
        try:
            self._state_path.write_text(json.dumps({
                "sent_hashes": self._sent_hashes,
                "last_sent_time": self._last_sent_time,
                "sends_today": self._sends_today,
                "sends_date": self._sends_date,
            }))
        except Exception as e:
            logger.warning("Heartbeat: failed to save state: %s", e)

    def _sends_used_today(self) -> int:
        today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
        if self._sends_date != today:
            self._sends_date = today
            self._sends_today = 0
        return self._sends_today

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """Start the heartbeat loop."""
        if self.running:
            return
        self.running = True
        self._task = asyncio.create_task(self._loop())
        logger.info(
            "Heartbeat started — checking every %d minutes",
            self.interval // 60,
        )

    def stop(self):
        """Stop the heartbeat loop."""
        self.running = False
        if self._task:
            self._task.cancel()
            self._task = None
        logger.info("Heartbeat stopped")

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    async def _loop(self):
        """Main heartbeat loop."""
        # Wait a bit on startup before first beat (let bot initialize)
        await asyncio.sleep(60)

        while self.running:
            try:
                if self._is_active_hours():
                    async with asyncio.timeout(300):  # 5 min max per beat
                        await self._beat()
                    self._consecutive_errors = 0
                else:
                    logger.debug("Heartbeat: outside active hours, skipping")

                await asyncio.sleep(self.interval)

            except asyncio.CancelledError:
                break
            except (asyncio.TimeoutError, TimeoutError) as e:
                logger.warning("Heartbeat timed out after 300s, killing orphaned processes")
                # Kill any Claude CLI processes spawned by this heartbeat
                try:
                    import subprocess
                    subprocess.run(
                        ["pkill", "-f", "claude.*--no-session-persistence"],
                        timeout=5, capture_output=True,
                    )
                except Exception:
                    pass
                try:
                    from goodman import learning
                    await learning.record_error("Heartbeat timed out after 300s")
                except Exception:
                    pass
                await asyncio.sleep(60)
                continue
            except Exception as e:
                self._consecutive_errors += 1
                backoff = min(
                    60 * (2 ** self._consecutive_errors), 3600
                )  # Max 1 hour
                logger.error(
                    "Heartbeat error (attempt %d, backoff %ds): %s",
                    self._consecutive_errors,
                    backoff,
                    e,
                )
                await asyncio.sleep(backoff)

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _is_active_hours(self) -> bool:
        """Check if current time is within active hours (user's timezone)."""
        now = datetime.now(ZoneInfo(TIMEZONE))
        return ACTIVE_HOURS_START <= now.hour < ACTIVE_HOURS_END

    def _is_duplicate(self, message: str) -> bool:
        """Check if this message is too similar to a recently sent one.

        Uses word-level overlap (same approach as fuzzy memory dedup).
        """
        # Tokenize the message
        msg_words = set(re.findall(r'[a-zA-Z0-9]+', message.lower()))
        if not msg_words:
            return False

        for prev_hash in self._sent_hashes:
            prev_words = set(prev_hash.split("|"))
            if not prev_words:
                continue
            overlap = len(msg_words & prev_words)
            smaller = min(len(msg_words), len(prev_words))
            if smaller > 0 and overlap / smaller > _DEDUP_SIMILARITY_THRESHOLD:
                return True

        return False

    def _record_sent(self, message: str):
        """Record a sent message for future dedup checks."""
        words = re.findall(r'[a-zA-Z0-9]+', message.lower())
        word_hash = "|".join(sorted(set(words)))
        self._sent_hashes.append(word_hash)
        # Keep bounded
        if len(self._sent_hashes) > self._max_sent_hashes:
            self._sent_hashes.pop(0)

    # ------------------------------------------------------------------
    # Core beat
    # ------------------------------------------------------------------

    async def _beat(self):
        """Execute one heartbeat cycle."""
        import time
        from goodman import brain, calendar, gmail, soul

        self._beat_count += 1

        # Load heartbeat tasks
        heartbeat_md = soul.load("heartbeat")
        if not heartbeat_md.strip():
            logger.debug("Heartbeat: no HEARTBEAT.md content, skipping")
            return

        # Gather context data concurrently
        context_parts = []
        now = datetime.now(ZoneInfo(TIMEZONE))

        # Check unread emails
        try:
            emails = await asyncio.to_thread(gmail.get_unread_emails, 5)
            if emails and emails.strip() and "no unread" not in emails.lower():
                context_parts.append(f"UNREAD EMAILS:\n{emails}")
        except Exception as e:
            error_str = str(e)
            if "invalid_grant" in error_str:
                # OAuth token expired — notify boss with re-auth instructions
                await self._notify_oauth_expired(error_str)
            logger.debug("Heartbeat email check failed: %s", e)

        # Check UPCOMING calendar events (not past ones)
        try:
            events = await asyncio.to_thread(calendar.get_upcoming_events_today)
            if events and events.strip() and "no events" not in events.lower() and "no upcoming" not in events.lower():
                context_parts.append(f"UPCOMING CALENDAR (remaining today):\n{events}")
        except Exception as e:
            logger.debug("Heartbeat calendar check failed: %s", e)

        # Check Slack DMs
        try:
            from goodman.config import SLACK_BOT_TOKEN
            if SLACK_BOT_TOKEN:
                from goodman import slack as slack_mod
                slack_dms = await asyncio.to_thread(slack_mod.get_unread_dms, 5)
                if slack_dms and slack_dms.strip() and "no recent" not in slack_dms.lower() and "not configured" not in slack_dms.lower():
                    context_parts.append(f"SLACK DMs:\n{slack_dms}")
        except Exception as e:
            logger.debug("Heartbeat Slack check failed: %s", e)

        # Check open tasks
        try:
            from goodman import tasks as tasks_mod
            active_tasks = await tasks_mod.format_tasks_for_context(TELEGRAM_ALLOWED_USER_ID)
            if active_tasks:
                context_parts.append(f"OPEN TASKS:\n{active_tasks}")
            blocked_review = await tasks_mod.format_blocked_for_review(TELEGRAM_ALLOWED_USER_ID)
            if blocked_review:
                context_parts.append(blocked_review)
        except Exception as e:
            logger.debug("Heartbeat task check failed: %s", e)

        # Check Trello board
        try:
            from goodman import trello
            if trello._configured():
                board_info = await asyncio.to_thread(trello.get_board_info)
                if board_info and "not configured" not in board_info.lower():
                    context_parts.append(f"TRELLO BOARD:\n{board_info}")
        except Exception as e:
            logger.debug("Heartbeat Trello check failed: %s", e)

        # Meeting-transcript ingestion (admin only, runs independently)
        if config.GRANOLA_ENABLED:
            try:
                from goodman import granola
                new_transcripts = await asyncio.to_thread(granola.fetch_new_transcripts)
                for t in new_transcripts:
                    await self._process_transcript(t)
            except Exception as e:
                logger.error("Granola sync failed: %s", e)

        # ------------------------------------------------------------------
        # PRE-FILTER: skip the brain call entirely if nothing to report
        # ------------------------------------------------------------------
        has_emails = any("UNREAD EMAILS" in p for p in context_parts)
        has_calendar = any("UPCOMING CALENDAR" in p for p in context_parts)
        has_slack = any("SLACK" in p for p in context_parts)

        # Check if calendar events are imminent (within 2 hours)
        has_imminent_event = False
        if has_calendar:
            for part in context_parts:
                if "UPCOMING CALENDAR" in part:
                    # Simple heuristic: check if any event time is within ~2h
                    import re as _re
                    times = _re.findall(r'(\d{1,2}:\d{2})', part)
                    for t in times:
                        try:
                            h, m = map(int, t.split(':'))
                            event_min = h * 60 + m
                            now_min = now.hour * 60 + now.minute
                            if 0 <= event_min - now_min <= 120:
                                has_imminent_event = True
                                break
                        except ValueError:
                            pass

        if not has_emails and not has_slack and not has_imminent_event:
            # Nothing actionable — skip entirely (costs zero tokens)
            logger.info("Heartbeat: pre-filter skip — no emails, no slack, no imminent events")
            return

        # ------------------------------------------------------------------
        # Cooldown + daily cap — imminent meeting warnings bypass both
        # (a 10:15 meeting warning must not be suppressed by a 9:00 ping).
        # ------------------------------------------------------------------
        if not has_imminent_event:
            if self._last_sent_time > 0:
                elapsed = time.time() - self._last_sent_time
                if 0 < elapsed < self._cooldown_seconds:
                    remaining = int((self._cooldown_seconds - elapsed) / 60)
                    logger.info("Heartbeat: in cooldown (%d min remaining), skipping", remaining)
                    return
            if self._sends_used_today() >= self._max_sends_per_day:
                logger.info("Heartbeat: daily cap reached (%d), skipping non-imminent beat",
                            self._max_sends_per_day)
                return

        context = (
            "\n\n".join(context_parts)
            if context_parts
            else "No new data from email or calendar."
        )

        # ------------------------------------------------------------------
        # HAIKU TRIAGE: cheap model decides if Opus is needed
        # ------------------------------------------------------------------
        triage_needed = await self._haiku_triage(context, now)
        if not triage_needed:
            logger.info("Heartbeat: Haiku triage says nothing actionable — skipping Opus")
            return

        # Build the heartbeat prompt
        time_str = now.strftime("%A, %B %d %Y, %I:%M %p %Z")
        prompt = (
            f"[HEARTBEAT CHECK — {time_str}]\n"
            f"This is an automated periodic check-in, NOT a user message.\n\n"
            f"Your heartbeat instructions:\n{heartbeat_md}\n\n"
            f"Current data:\n{context}\n\n"
            f"Instructions:\n"
            f"- Review the data above against your heartbeat tasks.\n"
            f"- CROSS-REFERENCE: If an email mentions a person with an upcoming calendar event, "
            f"check if it affects the event. Use tools to look up calendar events and availability.\n"
            f"- If someone cancels or proposes a new time, check the boss's calendar and propose a "
            f"specific reschedule with timezone conversion. Don't just flag — propose the action.\n"
            f"- If something needs the boss's DECISION or ACTION, write a concise proactive message.\n"
            f"- If NOTHING needs attention, respond with exactly: {HEARTBEAT_OK}\n"
            f"- NEVER send a digest: no schedule summaries, no lists of unread emails/DMs, "
            f"no portfolio updates. Only items that require the boss to decide or act. "
            f"When in doubt, {HEARTBEAT_OK}.\n"
            f"- Do NOT be chatty. Only reach out if there's something genuinely worth mentioning.\n"
            f"- Do NOT repeat information the boss already knows or was already notified about.\n"
            f"- Be Saul — punchy, useful, no fluff.\n"
            f"- Do NOT use markdown formatting.\n"
            f"- IMPORTANT: Only report calendar times EXACTLY as shown in the data above. "
            f"Do NOT guess or infer appointment times from memory or summaries.\n\n"
            f"TASK REVIEW (critical):\n"
            f"- Review any BLOCKED TASKS above. Think of alternative approaches.\n"
            f"- If a blocked task seems completable now (site back up, new approach), try it.\n"
            f"- If you think of a smart workaround, proactively tell the boss.\n"
            f"- If a task has 3+ failed attempts with no progress, suggest abandoning it "
            f"and offer an alternative.\n"
            f"- Use [TASK_DONE: description] when you complete a blocked task.\n"
            f"- Use [TASK: description] to create new tasks from your review.\n"
            f"- Use [TASK_BLOCKED: desc | reason: why | next: alternative] to update blocked tasks.\n"
            f"- Review the TRELLO BOARD if available — flag cards that need attention."
        )

        # Ask the brain
        user_id = TELEGRAM_ALLOWED_USER_ID
        if not user_id:
            logger.warning("Heartbeat: no TELEGRAM_ALLOWED_USER_ID configured")
            return

        # Use the queue system to acquire the user's lock for this system task.
        # Non-blocking: if user is busy, we skip this beat entirely.
        from goodman.bot import _message_queue

        q = await _message_queue.acquire_for_system(user_id)
        if q is None:
            logger.info("Heartbeat: skipping — user is busy")
            return

        try:
            try:
                response = await asyncio.wait_for(
                    brain.think(user_id, prompt, is_heartbeat=True),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                logger.warning("Heartbeat: brain.think timed out after 300s — skipping")
                response = None
        finally:
            _message_queue.release_for_system(user_id)

        # Extract task directives from heartbeat response
        if response:
            from goodman import tasks as tasks_mod
            await tasks_mod.extract_task_directives(user_id, response)

        # Analyze response
        if not response:
            logger.debug("Heartbeat: empty response")
            return

        clean = response.strip()

        # Check for HEARTBEAT_OK (case-insensitive, might be anywhere)
        if clean.upper().startswith(HEARTBEAT_OK):
            remainder = clean[len(HEARTBEAT_OK) :].strip().strip("-\u2014:").strip()
            if remainder and len(remainder) > 20:
                # There's a meaningful note after HEARTBEAT_OK — deliver it
                await self.send(remainder)
                soul.append_daily_log(f"[HEARTBEAT] {remainder[:200]}")
                logger.info("Heartbeat: delivered note after OK")
            else:
                logger.debug("Heartbeat: all clear")
            return

        if HEARTBEAT_OK in clean.upper():
            # HEARTBEAT_OK somewhere in middle/end — strip it out
            clean = (
                clean.replace(HEARTBEAT_OK, "")
                .replace("heartbeat_ok", "")
                .strip()
            )

        if clean:
            # Dedup check: don't send if too similar to a recently sent message
            if self._is_duplicate(clean):
                logger.info("Heartbeat: skipping duplicate message")
                return

            await self.send(clean)
            self._record_sent(clean)
            self._last_sent_time = time.time()  # Start cooldown
            self._sends_used_today()  # roll the date if needed
            self._sends_today += 1
            self._save_state()
            soul.append_daily_log(f"[HEARTBEAT] {clean[:200]}")
            logger.info("Heartbeat: delivered proactive message (%d/%d today, cooldown %d min)",
                        self._sends_today, self._max_sends_per_day,
                        self._cooldown_seconds // 60)
        else:
            logger.debug("Heartbeat: nothing to report after cleaning")

        # Every 8th beat, do memory curation + entity extraction (was 4th, doubled with 60min interval).
        # Hard outer timeouts so nothing in the curation chain can stall the heartbeat — and through it
        # the entire async event loop, including Telegram polling and Slack handling. Repeated hang
        # incidents (Apr 27, May 1) traced to inner subprocess/IO blocks that didn't honor cancellation.
        if self._beat_count % 8 == 0:
            try:
                await asyncio.wait_for(self._curate_memory(), timeout=420)
            except asyncio.TimeoutError:
                logger.warning("Memory curation: hit hard 420s outer timeout — skipping")
            except Exception as e:
                logger.warning("Memory curation: crashed (%s) — skipping", e)
            try:
                await asyncio.wait_for(self._extract_entities(), timeout=300)
            except asyncio.TimeoutError:
                logger.warning("Entity extraction: hit hard 300s outer timeout — skipping")
            except Exception as e:
                logger.warning("Entity extraction: crashed (%s) — skipping", e)

    async def _haiku_triage(self, context: str, now: datetime) -> bool:
        """Use Haiku to quickly decide if the context warrants an Opus call.

        Returns True if Opus should be called, False to skip.
        Costs ~1/60th of an Opus call.
        """
        import os

        time_str = now.strftime("%A, %B %d %Y, %I:%M %p")
        triage_prompt = (
            f"Time: {time_str}\n\n"
            f"Data from a periodic check-in:\n{context}\n\n"
            "Does ANY of the following apply? Apply a HIGH bar.\n"
            "1. A calendar event starting within the next 60 minutes, or a "
            "scheduling conflict / double-booking / cancellation affecting today\n"
            "2. An email or Slack DM that requires the boss's DECISION or REPLY "
            "today: a direct question from a colleague/partner/client, a deadline, "
            "something a person is blocked on. A merely unread message from a "
            "human does NOT count — FYIs, updates, and threads he can read later are NO.\n"
            "3. A genuine emergency: payment failure, security alert, account/service "
            "cutoff, production outage\n\n"
            "Reply YES or NO. Nothing else."
        )

        try:
            from goodman.brain import CLAUDE_CMD, PROJECT_ROOT
            env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}
            proc = await asyncio.create_subprocess_exec(
                CLAUDE_CMD,
                "--print",
                "--output-format", "text",
                "--model", "claude-haiku-4-5-20251001",
                "--no-session-persistence",
                "--max-turns", "0",
                "--append-system-prompt",
                "You are a triage filter. Answer YES or NO only.",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE,
                cwd=str(PROJECT_ROOT),
                env=env,
                start_new_session=True,
            )
            stdout, _ = await asyncio.wait_for(
                proc.communicate(input=triage_prompt.encode()), timeout=30
            )
            answer = stdout.decode().strip().upper()
            logger.info("Heartbeat Haiku triage: %s", answer[:20])
            return "YES" in answer
        except Exception as e:
            logger.warning("Haiku triage failed (%s), falling through to Opus", e)
            return True  # On failure, fall through to Opus (safe default)

    async def _curate_memory(self):
        """Review and curate memory files during idle time."""
        from goodman import brain, soul, db
        from goodman.config import TELEGRAM_ALLOWED_USER_ID
        from goodman.bot import _message_queue

        user_id = TELEGRAM_ALLOWED_USER_ID
        if not user_id:
            return

        # Use the queue system — non-blocking, skip if user is busy
        q = await _message_queue.acquire_for_system(user_id)
        if q is None:
            logger.info("Memory curation: skipping — user is busy")
            return

        logger.info("Memory curation cycle starting")

        try:
            logger.info("Memory curation: loading soul memory file")
            memory_md = soul.load("memory")
            logger.info("Memory curation: loading recent daily logs")
            daily_logs = soul.load_recent_daily_logs(3)
            logger.info("Memory curation: loading DB memories")
            memories = await asyncio.wait_for(db.get_memories(user_id), timeout=30)
            mem_count = len(memories) if memories else 0
            logger.info("Memory curation: loaded %d DB memories — building prompt", mem_count)

            # Hard skip when the corpus is too large to curate safely. Past 10k rows the
            # downstream brain.think → get_relevant_memories path runs synchronous BM25
            # over the entire corpus on the event-loop thread, blocking Telegram polling
            # for hours and ignoring asyncio.wait_for cancellation. May 1 + May 3 outages
            # both traced to this. Curation only adds value when MEMORY.md drifts from
            # daily logs; with 31k+ rows it can't actually re-summarize the whole corpus
            # in a single LLM call anyway.
            if mem_count > 10000:
                logger.warning(
                    "Memory curation: SKIPPED — corpus has %d memories (>10000 cap). "
                    "Curation logic was never designed for this scale; running it would "
                    "block the event loop. Consider running an offline pruning job instead.",
                    mem_count,
                )
                return

            if not daily_logs and not memories:
                logger.debug("Memory curation: nothing to curate")
                return

            mem_lines = "\n".join(
                f"- [{m['category']}] {m['content']}" for m in memories
            ) if memories else "(none)"

            prompt = (
                "[MEMORY CURATION — automated maintenance task]\n\n"
                "Review and organize your memory. Here is what you have:\n\n"
                f"MEMORY.md (your curated long-term memory):\n{memory_md}\n\n"
                f"Recent daily activity logs:\n{daily_logs}\n\n"
                f"Permanent memories from database:\n{mem_lines}\n\n"
                "Tasks:\n"
                "1. Update MEMORY.md with any new important facts from daily logs\n"
                "2. Remove duplicates or outdated information\n"
                "3. Organize by category (Key Facts, Projects, Preferences, Contacts)\n"
                "4. Keep it concise — this goes into your system prompt every time\n\n"
                "Return the updated MEMORY.md content wrapped in <memory> tags:\n"
                "<memory>\n... updated content ...\n</memory>\n\n"
                "If no changes needed, respond with exactly: MEMORY_OK"
            )

            try:
                response = await asyncio.wait_for(
                    brain.think(user_id, prompt, is_heartbeat=True),
                    timeout=300,
                )
            except asyncio.TimeoutError:
                logger.warning("Heartbeat: brain.think timed out after 300s — skipping")
                response = None
        finally:
            _message_queue.release_for_system(user_id)

        if not response or "MEMORY_OK" in response:
            logger.debug("Memory curation: no changes needed")
            return

        # Extract updated memory content from <memory> tags
        match = re.search(r'<memory>(.*?)</memory>', response, re.DOTALL)
        if match:
            new_memory = match.group(1).strip()
            if len(new_memory) > 50:  # Sanity check — don't save empty/tiny content
                soul.save("memory", new_memory)
                soul.append_daily_log("[CURATION] Updated MEMORY.md")
                logger.info("Memory curation: MEMORY.md updated (%d chars)", len(new_memory))
        else:
            logger.debug("Memory curation: no <memory> tags found in response")

    # ------------------------------------------------------------------
    # OAuth re-auth notification
    # ------------------------------------------------------------------

    _oauth_notified_at: float = 0  # Prevent spamming — only notify once per hour

    async def _notify_oauth_expired(self, error_str: str):
        """Notify the boss only for tokens that ACTUALLY fail to refresh.

        Verifies each token live (see _dead_oauth_accounts) instead of guessing
        the account from the error string, and sends a correctly-targeted re-auth
        link per dead account. If everything refreshes now (transient error), it
        stays silent instead of spamming a link for a healthy token.
        """
        import time as _time
        now = _time.monotonic()
        if now - self._oauth_notified_at < 3600:  # 1 hour cooldown
            return

        dead = await asyncio.to_thread(_dead_oauth_accounts)
        if not dead:
            logger.info("OAuth invalid_grant seen but all tokens refresh now — no notice sent")
            return
        self._oauth_notified_at = now

        names = ", ".join(em for em, _ in dead)
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
            from pathlib import Path

            oauth_client = Path("/home/claude/goodman/credentials/google-oauth.json")
            SCOPES = [
                'openid', 'https://www.googleapis.com/auth/userinfo.email',
                'https://www.googleapis.com/auth/calendar', 'https://mail.google.com/',
                'https://www.googleapis.com/auth/documents',
                'https://www.googleapis.com/auth/spreadsheets',
                'https://www.googleapis.com/auth/drive.readonly',
            ]
            blocks = []
            for em, _tf in dead:
                if oauth_client.exists():
                    flow = InstalledAppFlow.from_client_secrets_file(
                        str(oauth_client), SCOPES, redirect_uri='http://localhost:8085'
                    )
                    auth_url, _ = flow.authorization_url(
                        access_type='offline', prompt='consent', login_hint=em
                    )
                    blocks.append(f"• {em}:\n{auth_url}")
                else:
                    blocks.append(f"• {em}: re-auth needed but google-oauth.json not found")

            msg = (
                f"OAuth re-auth needed for: {names}. Email/calendar for THESE accounts "
                f"won't work until re-authorized (other accounts are fine). Sign in with "
                f"EACH account below, approve, paste the redirect URL back to me:\n\n"
                + "\n\n".join(blocks)
            )
            await self.send(msg)
            logger.warning("Sent OAuth re-auth notification for %s", names)
        except Exception as e:
            logger.error("Failed to generate OAuth re-auth URL: %s", e)
            await self.send(f"OAuth re-auth needed for: {names}. Manual re-auth needed.")

    # ------------------------------------------------------------------
    # Entity extraction cron — auto-populate knowledge graph
    # ------------------------------------------------------------------

    async def _extract_entities(self):
        """Scan recent conversations and memories for entity mentions.

        Auto-populates the knowledge graph with people, companies, and
        projects found in conversation text. Runs every 4th heartbeat
        (~2 hours). Inspired by Hyperspell's entity extraction cron.
        """
        try:
            from goodman import knowledge_graph as kg
            from goodman.config import TELEGRAM_ALLOWED_USER_ID
            import aiosqlite
            from goodman.config import DB_PATH

            user_id = TELEGRAM_ALLOWED_USER_ID
            if not user_id:
                return

            # Get recent messages (last 2 hours) that might contain entities
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT content FROM conversations "
                    "WHERE user_id = ? AND created_at >= datetime('now', '-2 hours') "
                    "AND role = 'assistant' "
                    "ORDER BY created_at DESC LIMIT 30",
                    (user_id,),
                )
                rows = await cursor.fetchall()

            if not rows:
                return

            # Also get recent memories for cross-referencing
            async with aiosqlite.connect(DB_PATH) as db:
                cursor = await db.execute(
                    "SELECT content, category FROM memories "
                    "WHERE user_id = ? AND created_at >= datetime('now', '-2 hours') "
                    "AND category IN ('contact', 'general', 'project', 'work') "
                    "ORDER BY created_at DESC LIMIT 20",
                    (user_id,),
                )
                memory_rows = await cursor.fetchall()

            # Extract entities using regex patterns
            import re
            all_text = " ".join(r[0] for r in rows)
            if memory_rows:
                all_text += " " + " ".join(r[0] for r in memory_rows)

            entities_added = 0

            # Extract emails → create person entities
            emails = set(re.findall(r'([\w.+-]+@[\w.-]+\.\w{2,})', all_text))
            for email in emails:
                # Skip system/generic emails
                if any(x in email for x in ['noreply', 'notification', 'system', 'bot']):
                    continue
                # Check if entity exists
                existing = await kg.find_entity(email.split('@')[0])
                if not existing:
                    name = email.split('@')[0].replace('.', ' ').replace('_', ' ').title()
                    eid = await kg.upsert_entity(name, "person", {"email": email})
                    await kg.auto_generate_aliases(eid, name, {"email": email})
                    entities_added += 1

            # Extract Telegram handles
            handles = set(re.findall(r'@(\w{4,30})', all_text))
            for handle in handles:
                # Skip common non-person handles
                if handle.lower() in ('gmail', 'acme', 'everyone', 'channel', 'here'):
                    continue
                existing = await kg.find_entity(handle)
                if not existing:
                    eid = await kg.upsert_entity(handle, "person", {"telegram": f"@{handle}"})
                    await kg.auto_generate_aliases(eid, handle, {"telegram": f"@{handle}"})
                    entities_added += 1

            if entities_added > 0:
                logger.info("Entity extraction: added %d new entities to knowledge graph", entities_added)

        except Exception as e:
            logger.debug("Entity extraction failed: %s", e)

    # ------------------------------------------------------------------
    # Granola transcript processing
    # ------------------------------------------------------------------

    async def _process_transcript(self, transcript: dict):
        """Extract key facts from a meeting transcript and save to memory."""
        from goodman import brain, db, soul

        user_id = TELEGRAM_ALLOWED_USER_ID
        if not user_id:
            return

        title = transcript["title"]
        date = transcript["date"]
        text = transcript["transcript_text"][:8000]

        prompt = (
            f"Meeting: {title} ({date})\n\n"
            f"TRANSCRIPT:\n{text}\n\n"
            "Extract the key facts, decisions, action items, and important details "
            "from this meeting transcript. Return each as a separate line starting "
            "with [REMEMBER: ...]. Focus on:\n"
            "- Decisions made\n- Action items and who owns them\n"
            "- Key dates/deadlines mentioned\n- Important facts or numbers\n"
            "- People mentioned and their roles/context"
        )

        system_prompt = (
            "You are a meeting transcript analyzer. Extract key facts as "
            "[REMEMBER: ...] directives, one per line. Be specific and factual."
        )

        response = await brain._call_claude(system_prompt, prompt, max_turns=0)

        # Extract [REMEMBER:] lines and save with meeting prefix
        memories = brain._extract_memories_from_text(response)
        for category, content in memories:
            prefixed = f"[Meeting: {title}] {content}"
            await db.save_memory_fuzzy(user_id, category, prefixed)

        # Log to daily episodic memory
        soul.append_daily_log(
            f"[GRANOLA] Processed transcript: {title} "
            f"({date}) — {len(memories)} memories extracted"
        )

        logger.info("Granola: processed '%s', %d memories", title, len(memories))

    # ------------------------------------------------------------------
    # Manual trigger
    # ------------------------------------------------------------------

    async def force_beat(self):
        """Force an immediate heartbeat check (ignores active hours)."""
        logger.info("Heartbeat: forced check triggered")
        await self._beat()
