"""The Goodman Protocol - Telegram Bot (Command Center)."""

import asyncio
import logging
import os
import re
import signal
import time as _time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from goodman.config import (
    TELEGRAM_BOT_TOKEN, TELEGRAM_ALLOWED_USER_ID, TIMEZONE, PROJECT_ROOT,
    SLACK_BOT_TOKEN, SLACK_APP_TOKEN, SLACK_PRIVILEGED_USERS, SLACK_OWNER_USER_ID, SLACK_RESTRICTED_CHANNELS,
    SLACK_BOARD_DATA_USERS,
    WHISPER_LANGUAGE, TTS_ENABLED,
    TELETHON_API_ID, TELETHON_API_HASH, WA_ENABLED, CONSULATE_MONITOR_ENABLED,
)
from goodman import agents, brain, coder, db, calendar, file_ingest, gmail, slack, soul, transcribe, vision, web
from goodman import config
from goodman import reply_flow
from goodman.heartbeat import Heartbeat
from goodman.scheduler import Scheduler
from goodman.msgqueue import MessageQueue, MessageBatch
from goodman import session as session_mod

TELEGRAM_MAX_LENGTH = 4096
PID_FILE = PROJECT_ROOT / "data" / "goodman.pid"

# Global instances (initialized in post_init)
_heartbeat: Heartbeat | None = None
_scheduler: Scheduler | None = None
_slack_listener = None  # SlackListener | None
_tg_listener = None     # TelegramUserbotListener | None
_wa_listener = None     # WhatsAppListener | None
_consulate_monitor = None  # ConsulateMonitor | None
_send_proactive = None  # async callable(text) → Telegram, set in post_init()

# Global message queue — replaces old debounce/lock/active_processing system
_message_queue = MessageQueue(debounce_seconds=2.0, max_queue_size=20)

# Processed update IDs — prevents duplicate processing across restarts
_processed_updates: set[int] = set()
_MAX_PROCESSED_IDS = 500  # Keep last N IDs in memory

# Context storage — maps update_id to ContextTypes.DEFAULT_TYPE
# (Telegram Update objects are frozen, can't set attributes on them)
_update_contexts: dict[int, object] = {}


# --- Cross-platform reply directives ---
# Brain outputs [TG_REPLY:tg_ID:Display Name]...[/TG_REPLY] when it wants to send a TG DM.
# bot.py intercepts these, creates PendingReply, and shows inline keyboard confirmation.
# Accepts tg_<numeric_id> OR @username as the target.
_TG_REPLY_RE = re.compile(
    r'\[TG_REPLY:(tg_\d+|@\w+):([^\]]+)\]\s*\n(.*?)\[/TG_REPLY\]',
    re.DOTALL,
)
_WA_REPLY_RE = re.compile(
    r'\[WA_REPLY:([\d+]+):([^\]]+)\]\s*\n(.*?)\[/WA_REPLY\]',
    re.DOTALL,
)


# --- Stream filter: demuxes LLM responses into user text vs directives ---

class _StreamFilter:
    """Directive-aware stream filter for Telegram delivery.

    Buffers streamed chunks and suppresses content within directive blocks
    ([TG_REPLY:...], [WA_REPLY:...], [REMEMBER:...]). Non-directive text
    is forwarded to the send function immediately.
    """

    _OPEN_TAGS = ("[TG_REPLY:", "[WA_REPLY:")
    _CLOSE_TAGS = ("[/TG_REPLY]", "[/WA_REPLY]")

    def __init__(self, send_fn):
        self._send = send_fn
        self._buffer = ""
        self._suppressing = False
        self.sent_any = False

    async def __call__(self, chunk: str):
        if not chunk:
            return
        self._buffer += chunk
        await self._flush_safe()

    async def finish(self):
        if self._buffer and not self._suppressing:
            clean = self._clean(self._buffer)
            if clean.strip():
                await self._send(clean)
                self.sent_any = True
        self._buffer = ""

    async def _flush_safe(self):
        while self._buffer:
            if self._suppressing:
                close_pos, close_len = self._find_earliest(self._CLOSE_TAGS)
                if close_pos >= 0:
                    self._buffer = self._buffer[close_pos + close_len:]
                    self._suppressing = False
                    continue
                return

            open_pos, _ = self._find_earliest(self._OPEN_TAGS)
            if open_pos >= 0:
                before = self._buffer[:open_pos]
                self._buffer = self._buffer[open_pos:]
                self._suppressing = True
                clean = self._clean(before)
                if clean.strip():
                    await self._send(clean)
                    self.sent_any = True
                continue

            # Only hold back if the buffer tail could be a partial tag
            # (all directive tags start with '[')
            bracket_pos = self._buffer.rfind("[")
            if bracket_pos >= 0:
                tail = self._buffer[bracket_pos:]
                all_tags = self._OPEN_TAGS + self._CLOSE_TAGS
                if any(tag.startswith(tail) for tag in all_tags):
                    to_send = self._buffer[:bracket_pos]
                    self._buffer = tail
                    if to_send:
                        clean = self._clean(to_send)
                        if clean.strip():
                            await self._send(clean)
                            self.sent_any = True
                    return

            # No partial tag — send everything
            clean = self._clean(self._buffer)
            self._buffer = ""
            if clean.strip():
                await self._send(clean)
                self.sent_any = True
            return

    def _find_earliest(self, tags):
        best_pos, best_len = -1, 0
        for tag in tags:
            pos = self._buffer.find(tag)
            if pos >= 0 and (best_pos < 0 or pos < best_pos):
                best_pos = pos
                best_len = len(tag)
        return best_pos, best_len

    @staticmethod
    def _clean(text):
        lines = text.split("\n")
        lines = [l for l in lines if not l.strip().startswith("[REMEMBER:")
                 and not l.strip().startswith("[TASK:")
                 and not l.strip().startswith("[TASK_DONE:")
                 and not l.strip().startswith("[TASK_BLOCKED:")
                 and not l.strip().startswith("[SEND_FILE:")]
        text = "\n".join(lines)
        text = brain._strip_leaked_internals(text)
        return brain._strip_markdown(text)


# Missed-message digest settings
_DIGEST_INTERVAL = 3600       # seconds between digest checks (1 hour)
_DIGEST_QUIET_START = 23      # don't send digests after 11pm
_DIGEST_QUIET_END = 8         # don't send digests before 8am
_digest_since: str | None = None  # ISO timestamp of last digest check


def is_user_processing(user_id: str) -> bool:
    """Check if a user's message is currently being processed.

    Used by the heartbeat to avoid firing brain.think() concurrently
    with an active message handler, which causes context pollution.
    """
    return _message_queue.is_user_processing(user_id)


def is_user_locked(user_id: str) -> bool:
    """Check if the user's queue lock is held."""
    return _message_queue.is_user_locked(user_id)


def serialized(func):
    """Decorator: acquire per-user queue lock for command handlers.

    Apply to ALL handlers that call brain.think() — both command handlers
    and the regular message handler — so that only one brain call per user
    runs at a time. This prevents the response-spillover bug.
    """
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        q = _message_queue._get_queue(user_id)
        async with q._lock:
            q._processing = True
            try:
                return await func(update, context)
            finally:
                q._processing = False
    return wrapper

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)


def _acquire_pid_lock():
    """Ensure only one bot instance runs. Kill any previous instance."""
    if PID_FILE.exists():
        try:
            old_pid = int(PID_FILE.read_text().strip())
            # Check if old process is still running
            os.kill(old_pid, 0)  # signal 0 = just check if alive
            logger.warning("Killing previous bot instance (PID %d)", old_pid)
            os.kill(old_pid, signal.SIGTERM)
            # Wait for it to die
            import time
            for _ in range(10):
                try:
                    os.kill(old_pid, 0)
                    time.sleep(0.5)
                except OSError:
                    break
            else:
                # Force kill if still alive
                try:
                    os.kill(old_pid, signal.SIGKILL)
                except OSError:
                    pass
        except (OSError, ValueError):
            pass  # Process already dead or invalid PID

    PID_FILE.write_text(str(os.getpid()))
    logger.info("PID lock acquired: %d", os.getpid())


def authorized(func):
    """Decorator to restrict access to the allowed user only."""
    async def wrapper(update: Update, context: ContextTypes.DEFAULT_TYPE):
        user_id = str(update.effective_user.id)
        if TELEGRAM_ALLOWED_USER_ID and user_id != TELEGRAM_ALLOWED_USER_ID:
            await update.message.reply_text(
                "Sorry pal, I only take calls from my client. "
                "You got the wrong number."
            )
            return
        return await func(update, context)
    return wrapper


async def _send_message(update: Update, text: str):
    """Send a message, splitting into chunks if it exceeds Telegram's limit."""
    while text:
        chunk = text[:TELEGRAM_MAX_LENGTH]
        await update.message.reply_text(chunk)
        text = text[TELEGRAM_MAX_LENGTH:]


async def _send_pending_files(update: Update):
    """Send any files queued by brain via [SEND_FILE:] directives."""
    files = brain.drain_pending_files()
    for path in files:
        try:
            ext = os.path.splitext(path)[1].lower()
            with open(path, "rb") as f:
                if ext in (".png", ".jpg", ".jpeg", ".gif", ".webp"):
                    await update.message.reply_photo(photo=f)
                else:
                    await update.message.reply_document(document=f)
            logger.info("Sent file: %s", path)
        except Exception as e:
            logger.error("Failed to send file %s: %s", path, e)


@authorized
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command."""
    user_id = str(update.effective_user.id)
    await db.save_message(user_id, "assistant", "Better Call Saul! What can I do for you today, counselor?")
    await update.message.reply_text(
        "Better Call Saul! What can I do for you today, counselor?\n\n"
        "Here's what I can do for you:\n"
        "/brief - Get your morning docket\n"
        "/calendar - Check today's schedule\n"
        "/email - Check your inbox\n"
        "/slack - Check Slack messages\n"
        "/code - Kick off a coding job\n"
        "/research - Get intel on a topic\n"
        "/soul - View my soul files\n"
        "/tasks - View open task queue\n"
        "/heartbeat - Force a proactive check\n"
        "/jobs - View scheduled reminders\n"
        "/cancel - Cancel a scheduled job\n\n"
        "Or just talk to me like a normal human being.\n"
        "Try: 'remind me tomorrow at 3pm to call John'\n"
        "S'all good, man."
    )


@authorized
@serialized
async def cmd_brief(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /brief command - morning briefing."""
    user_id = str(update.effective_user.id)
    await update.message.reply_text(
        "Pulling together your morning docket, boss. Give me a moment..."
    )
    cal_data = await asyncio.to_thread(calendar.get_todays_events)
    email_data = await asyncio.to_thread(gmail.get_unread_emails, 5)

    # Gather missed cross-platform messages (last 24 hours)
    global _digest_since
    tz = ZoneInfo(TIMEZONE)
    yesterday = (datetime.now(tz) - timedelta(hours=24)).isoformat()
    missed_msgs = await db.get_cross_platform_messages_since(yesterday, limit=20)
    if missed_msgs:
        missed_lines = []
        for msg in missed_msgs:
            source = msg["user_id"].split("_")[0].upper()
            missed_lines.append(f"[{source}] {msg['content'][:200]}")
        missed_data = "\n".join(missed_lines)
        # Reset digest timer since briefing covers it
        _digest_since = datetime.now(tz).isoformat()
    else:
        missed_data = "No missed messages."

    # Portfolio snapshot (owner-only; boss is the owner here)
    portfolio_data = "Unavailable."
    try:
        from goodman import ibkr
        portfolio_data = await asyncio.to_thread(ibkr.render_report)
    except Exception:
        pass

    response = await brain.think(
        user_id,
        f"The user wants their morning briefing. Here is their data:\n\n"
        f"CALENDAR:\n{cal_data}\n\n"
        f"UNREAD EMAILS:\n{email_data}\n\n"
        f"MISSED DMs (Slack/Telegram DMs/WhatsApp from last 24h — direct messages only):\n{missed_data}\n\n"
        f"PORTFOLIO (IBKR, live):\n{portfolio_data}\n\n"
        "Present a Saul Goodman-style morning docket per your HEARTBEAT portfolio rules: "
        "1) Today's calendar events, 2) Email inbox status (highlight urgent ones), "
        "3) Missed DMs summary (only things needing attention — do NOT compile Telegram group/channel chatter), "
        "4) PORTFOLIO: CORE vs SWING readout (swings show weeks-held vs 2-8wk window, next catalyst, and sell/exit guidance), "
        "5) A motivational Saul quote."
    )
    await db.save_message(user_id, "user", "/brief")
    await db.save_message(user_id, "assistant", response)
    await _send_message(update, response)


@authorized
@serialized
async def cmd_calendar(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /calendar command."""
    user_id = str(update.effective_user.id)
    await update.message.reply_text(
        "Let me check the docket, counselor..."
    )
    cal_today = await asyncio.to_thread(calendar.get_todays_events)
    cal_upcoming = await asyncio.to_thread(calendar.get_upcoming_events, 7)
    response = await brain.think(
        user_id,
        f"The user wants to check their calendar. Here's the data:\n\n"
        f"{cal_today}\n\n{cal_upcoming}\n\n"
        "Present this in Saul Goodman style. Be concise."
    )
    await db.save_message(user_id, "user", "/calendar")
    await db.save_message(user_id, "assistant", response)
    await _send_message(update, response)


@authorized
@serialized
async def cmd_email(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /email command."""
    user_id = str(update.effective_user.id)
    await update.message.reply_text(
        "Rifling through the evidence pile, boss..."
    )
    args = " ".join(context.args) if context.args else ""

    if args:
        # Search mode
        email_data = await asyncio.to_thread(gmail.search_emails, args, 5)
        prompt = (
            f"The user searched their email for: '{args}'. Here are the results:\n\n"
            f"{email_data}\n\n"
            "Present the findings in Saul Goodman style."
        )
    else:
        # Overview mode
        email_data = await asyncio.to_thread(gmail.get_unread_emails, 10)
        prompt = (
            f"The user wants to check their inbox. Here are the unread emails:\n\n"
            f"{email_data}\n\n"
            "Summarize the inbox in Saul Goodman style. Highlight anything that looks "
            "urgent or important. Mention the sender and subject for each."
        )

    response = await brain.think(user_id, prompt)
    await db.save_message(user_id, "user", f"/email {args}".strip())
    await db.save_message(user_id, "assistant", response)
    await _send_message(update, response)


@authorized
@serialized
async def cmd_code(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /code command — execute coding tasks via Claude CLI."""
    user_id = str(update.effective_user.id)
    task_text = " ".join(context.args) if context.args else ""
    if not task_text:
        await update.message.reply_text(
            "You gotta give me something to work with, counselor. "
            "Try: /code fix the login bug in auth.py"
        )
        return

    is_self_improve = False  # Self-improvement via /code --self-improve flag only
    mode = "self-improvement" if is_self_improve else "coding"

    await update.message.reply_text(
        f"On it, boss. Firing up the legal team for: \"{task_text}\"\n"
        f"Mode: {mode}. I'll report back when we've got something."
    )
    await db.save_message(user_id, "user", f"/code {task_text}")

    result = await coder.execute_coding_task(task_text, is_self_improve=is_self_improve)
    logger.info("Coding result: success=%s, turns=%s, cost=$%s",
                result["success"], result["num_turns"], result["cost_usd"])

    if result["success"]:
        # Deliver coder's detailed result directly — don't re-process through brain
        response = (
            f"Job done, boss. Here's the full report:\n\n"
            f"{result['result']}\n\n"
            f"Cost: ${result['cost_usd']:.4f} | Turns: {result['num_turns']}"
        )
    else:
        # Only use brain for failures where Saul needs to explain what went wrong
        response = await brain.think(
            user_id,
            f"You just ran a {mode} task for the user: \"{task_text}\"\n\n"
            f"IT FAILED. Result:\n{result['result']}\n\n"
            f"Cost: ${result['cost_usd']:.4f}, Turns: {result['num_turns']}\n\n"
            "Explain honestly what went wrong and suggest next steps."
        )
    await db.save_message(user_id, "assistant", response)
    await _send_message(update, response)

    if is_self_improve and result["success"]:
        await update.message.reply_text(
            "Alright, restarting myself to apply the changes. "
            "Back in a flash, counselor!"
        )
        coder.restart_bot()


@authorized
async def cmd_soul(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /soul command — view or manage soul files."""
    args = " ".join(context.args) if context.args else ""

    if not args:
        # List all soul files with previews
        parts = ["The Goodman Protocol — Soul Files\n"]
        for name in ["soul", "user", "heartbeat", "memory"]:
            content = soul.load(name)
            if content:
                preview = content.strip()[:150].replace("\n", " ")
                parts.append(f"{name.upper()}.md: {preview}...")
            else:
                parts.append(f"{name.upper()}.md: (empty)")
        await _send_message(update, "\n\n".join(parts))
        return

    # View a specific soul file
    name = args.lower().replace(".md", "")
    content = soul.load(name)
    if content:
        await _send_message(update, f"=== {name.upper()}.md ===\n\n{content}")
    else:
        await update.message.reply_text(
            f"No soul file named '{name}', counselor. "
            f"Available: soul, user, heartbeat, memory"
        )


@authorized
@serialized
async def cmd_heartbeat(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /heartbeat command — force a heartbeat check."""
    if _heartbeat is None:
        await update.message.reply_text(
            "Heartbeat system isn't running, boss. Check the logs."
        )
        return
    await update.message.reply_text(
        "Running a heartbeat check right now, counselor. Stand by..."
    )
    await _heartbeat.force_beat()


@authorized
async def cmd_tasks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /tasks command — list open tasks."""
    user_id = str(update.effective_user.id)
    from goodman import tasks as tasks_mod

    tasks_text = await tasks_mod.format_tasks_for_context(user_id)
    if not tasks_text:
        await _send_message(update, "No open tasks on the board, counselor. Clean slate.")
        return
    await _send_message(update, tasks_text)


@authorized
async def cmd_consulate(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /consulate command — force a consulate slot check."""
    if _consulate_monitor is None:
        await update.message.reply_text("Consulate monitor isn't running, boss.")
        return
    await update.message.reply_text("Checking all consulates for available slots, boss. Stand by...")
    result = await _consulate_monitor.force_check()
    await _send_message(update, result)


@authorized
async def cmd_jobs(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /jobs command — list scheduled jobs."""
    if _scheduler is None:
        await update.message.reply_text("Scheduler isn't running, boss.")
        return

    jobs = await _scheduler.list_jobs()
    if not jobs:
        await update.message.reply_text(
            "No scheduled jobs on the docket, counselor. "
            "You can set one with something like: 'remind me tomorrow at 3pm to call John'"
        )
        return

    lines = ["Your scheduled jobs, counselor:\n"]
    for job in jobs:
        lines.append(
            f"#{job['id']} — {job['name']}\n"
            f"  Type: {job['schedule_type']}, Next: {job['next_run_at'][:16]}\n"
            f"  Payload: {job['payload'][:80]}"
        )
    await _send_message(update, "\n\n".join(lines))


@authorized
async def cmd_cancel_job(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /cancel command — cancel a scheduled job by ID."""
    if _scheduler is None:
        await update.message.reply_text("Scheduler isn't running, boss.")
        return

    args = " ".join(context.args) if context.args else ""
    if not args or not args.strip().isdigit():
        await update.message.reply_text(
            "Give me the job number, counselor. "
            "Check /jobs for the list, then: /cancel 3"
        )
        return

    job_id = int(args.strip())
    await _scheduler.remove_job(job_id)
    await update.message.reply_text(
        f"Job #{job_id} cancelled. Consider it struck from the record, boss."
    )


@authorized
@serialized
async def cmd_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /reset command — manually reset the conversation session."""
    user_id = str(update.effective_user.id)
    new_session_id = await session_mod.reset_session(user_id)
    await update.message.reply_text(
        f"Fresh session started, counselor. Clean slate, new case. "
        f"(Session #{new_session_id})"
    )


@authorized
@serialized
async def cmd_research(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /research command."""
    user_id = str(update.effective_user.id)
    topic = " ".join(context.args) if context.args else ""
    if not topic:
        await update.message.reply_text(
            "Research what, exactly? Even Saul needs a lead. "
            "Try: /research latest AI funding rounds"
        )
        return

    await update.message.reply_text(
        f"I'm putting my PI on it: \"{topic}\"\n"
        "Let me make some calls..."
    )
    search_results = await web.search_web(topic)
    response = await brain.think(
        user_id,
        f"The user wants research on: {topic}.\n\n"
        f"[WEB SEARCH RESULTS:\n{search_results}]\n\n"
        "Summarize the findings in Saul Goodman style. Be informative and actionable."
    )
    await db.save_message(user_id, "user", f"/research {topic}")
    await db.save_message(user_id, "assistant", response)
    await _send_message(update, response)


@authorized
@serialized
async def cmd_slack(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /slack command — Slack status overview."""
    user_id = str(update.effective_user.id)
    await update.message.reply_text(
        "Checking the Slack wire, counselor..."
    )
    args = " ".join(context.args) if context.args else ""

    if args:
        # Search mode
        slack_data = await asyncio.to_thread(slack.search_messages, args, 10)
        prompt = (
            f"The user searched Slack for: '{args}'. Here are the results:\n\n"
            f"{slack_data}\n\n"
            "Present the findings in Saul Goodman style."
        )
    else:
        # Overview mode — DMs + channel list
        dms = await asyncio.to_thread(slack.get_unread_dms, 10)
        channels = await asyncio.to_thread(slack.list_channels)
        prompt = (
            f"The user wants a Slack overview. Here's the data:\n\n"
            f"RECENT DMs:\n{dms}\n\n"
            f"CHANNELS:\n{channels}\n\n"
            "Summarize the Slack status in Saul Goodman style. Be concise."
        )

    response = await brain.think(user_id, prompt)
    await db.save_message(user_id, "user", f"/slack {args}".strip())
    await db.save_message(user_id, "assistant", response)
    await _send_message(update, response)


@authorized
async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle regular text messages — enqueue into the MessageQueue system."""
    # De-duplicate: skip if we've already processed this update
    update_id = update.update_id
    if update_id in _processed_updates:
        logger.debug("Skipping duplicate update %d", update_id)
        return
    _processed_updates.add(update_id)
    # Trim the set to prevent unbounded growth
    if len(_processed_updates) > _MAX_PROCESSED_IDS:
        # Remove oldest entries (smallest IDs)
        to_remove = sorted(_processed_updates)[:_MAX_PROCESSED_IDS // 2]
        _processed_updates.difference_update(to_remove)

    user_id = str(update.effective_user.id)
    message = update.message.text

    await update.message.chat.send_action("typing")

    # Detect YouTube URLs — extract transcript and enrich the message
    yt_url = file_ingest.extract_youtube_url(message)
    if yt_url:
        result = await file_ingest.ingest_youtube(user_id, yt_url)
        if result["success"]:
            transcript_preview = result["transcript"][:2000]
            await db.save_message(user_id, "user", f"[YouTube video: {result['title']}]")
            await db.save_message(user_id, "assistant",
                f"Indexed '{result['title']}' — {result['chunks']} chunks, "
                f"{result['chars']:,} characters in memory.")
            # Enrich the user's message with transcript context
            message = (
                f"{message}\n\n"
                f"[TRANSCRIPT of '{result['title']}' ({result['chars']:,} chars, "
                f"{result['chunks']} chunks indexed in memory):\n"
                f"{transcript_preview}\n"
                f"... full transcript in vector memory, use it to answer.]"
            )
        else:
            # Transcript extraction failed — pass URL to brain as-is
            message = f"{message}\n\n[YouTube transcript extraction failed: {result['error']}. Try fetching the page or asking the boss for context.]"

    # Detect X/Twitter URLs — extract video transcript and enrich the message
    x_url = file_ingest.extract_x_url(message)
    if x_url and not yt_url:
        result = await file_ingest.ingest_x_video(user_id, x_url)
        if result.get("success") and result.get("has_video"):
            transcript_preview = result["transcript"][:2000]
            await db.save_message(user_id, "user", f"[X video: {x_url}]")
            await db.save_message(user_id, "assistant",
                f"Indexed X video — {result['chunks']} chunks, "
                f"{result['chars']:,} characters in memory.")
            message = (
                f"{message}\n\n"
                f"[X VIDEO TRANSCRIPT ({result['chars']:,} chars, "
                f"{result['chunks']} chunks indexed in memory):\n"
                f"{transcript_preview}\n"
                f"... full transcript in vector memory, use it to answer.]"
            )
        elif result.get("success") and result.get("tweet_text"):
            message = f"{message}\n\n[X post text (no video found): {result['tweet_text']}]"

    # Store context for later retrieval by the batch handler
    # (Update objects are frozen — use a separate dict)
    _update_contexts[update.update_id] = context

    # Enqueue into the queue system — handles debouncing, batching, follow-ups
    await _message_queue.enqueue(user_id, message, update)


@authorized
async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle voice messages — transcribe and enqueue as text."""
    # De-duplicate
    update_id = update.update_id
    if update_id in _processed_updates:
        logger.debug("Skipping duplicate voice update %d", update_id)
        return
    _processed_updates.add(update_id)
    if len(_processed_updates) > _MAX_PROCESSED_IDS:
        to_remove = sorted(_processed_updates)[:_MAX_PROCESSED_IDS // 2]
        _processed_updates.difference_update(to_remove)

    user_id = str(update.effective_user.id)
    await update.message.chat.send_action("typing")

    # Download voice file
    voice_file = await update.message.voice.get_file()
    audio_bytes = await voice_file.download_as_bytearray()

    # Transcribe in a thread (CPU-bound)
    transcript = await asyncio.to_thread(
        transcribe.transcribe, bytes(audio_bytes), WHISPER_LANGUAGE
    )

    if not transcript:
        await update.message.reply_text(
            "Couldn't make out what you said, boss. Try again?"
        )
        return

    logger.info("Voice transcribed (%d chars): %s", len(transcript), transcript[:100])

    # Prefix and enqueue into normal message pipeline
    message = f"[Voice message transcription]: {transcript}"
    _update_contexts[update.update_id] = context
    await _message_queue.enqueue(user_id, message, update)


_SHARE_RE = re.compile(r'(^|\s)(#share\b|\[share\])', re.IGNORECASE)

def _visibility_from_caption(caption: str) -> str:
    """Caption-driven visibility for ingested files.

    'shared' if the caption contains '#share' or '[SHARE]' (case-insensitive).
    'personal' otherwise — the safe default.
    """
    if caption and _SHARE_RE.search(caption):
        return "shared"
    return "personal"


@authorized
async def handle_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle audio file messages (m4a, mp3 etc.) — transcribe via Whisper."""
    update_id = update.update_id
    if update_id in _processed_updates:
        return
    _processed_updates.add(update_id)
    if len(_processed_updates) > _MAX_PROCESSED_IDS:
        to_remove = sorted(_processed_updates)[:_MAX_PROCESSED_IDS // 2]
        _processed_updates.difference_update(to_remove)

    user_id = str(update.effective_user.id)
    audio = update.message.audio
    if not audio:
        return

    filename = audio.file_name or f"audio.{audio.mime_type.split('/')[-1] if audio.mime_type else 'm4a'}"
    await update.message.chat.send_action("typing")

    try:
        tg_file = await audio.get_file()
        audio_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.error("Failed to download audio '%s': %s", filename, e)
        await update.message.reply_text("Couldn't download that audio file, boss.")
        return

    transcript = await asyncio.to_thread(
        transcribe.transcribe, audio_bytes, WHISPER_LANGUAGE
    )
    if not transcript:
        await update.message.reply_text(
            f"Couldn't transcribe '{filename}', boss. The audio might be empty or corrupted."
        )
        return

    logger.info("Audio file transcribed '%s' (%d chars): %s", filename, len(transcript), transcript[:100])

    caption = update.message.caption or ""
    visibility = _visibility_from_caption(caption)

    # Ingest transcript into vector memory
    result = await file_ingest.ingest_file(
        user_id, transcript.encode("utf-8"), f"{filename}.txt", visibility=visibility,
    )
    tag = " [SHARED with Slack]" if visibility == "shared" else ""
    status = (
        f"Transcribed '{filename}' — {len(transcript):,} characters."
        + (f" Indexed in {result['chunks']} chunks.{tag}" if result.get("success") else "")
    )
    await update.message.reply_text(status)
    await db.save_message(user_id, "user", f"[Audio file: {filename}]")
    await db.save_message(user_id, "assistant", status)

    message = (
        f"[Audio file '{filename}' transcribed ({len(transcript):,} chars), "
        f"indexed in memory.]\n\nTranscript:\n{transcript[:3000]}"
    )
    if caption:
        message += f"\n\nUser's note: {caption}"
    _update_contexts[update.update_id] = context
    await _message_queue.enqueue(user_id, message, update)


_UPLOADS_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "uploads"


def _save_upload(file_bytes: bytes, filename: str) -> str:
    """Persist an uploaded file to data/uploads/ so the brain can reference it
    by path later (e.g. to push a CV to DemoATS via demoats_cli --resume).
    Returns the absolute path."""
    _UPLOADS_DIR.mkdir(parents=True, exist_ok=True)
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", filename) or "file"
    path = _UPLOADS_DIR / f"{int(_time.time())}_{safe}"
    path.write_bytes(file_bytes)
    return str(path)


@authorized
async def handle_document(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle file/document uploads — extract text, chunk, embed, store in vector memory."""
    # De-duplicate
    update_id = update.update_id
    if update_id in _processed_updates:
        logger.debug("Skipping duplicate document update %d", update_id)
        return
    _processed_updates.add(update_id)
    if len(_processed_updates) > _MAX_PROCESSED_IDS:
        to_remove = sorted(_processed_updates)[:_MAX_PROCESSED_IDS // 2]
        _processed_updates.difference_update(to_remove)

    user_id = str(update.effective_user.id)
    doc = update.message.document
    if not doc:
        return

    filename = doc.file_name or "unknown_file"
    file_size = doc.file_size or 0

    await update.message.chat.send_action("typing")

    # Download the file
    try:
        tg_file = await doc.get_file()
        file_bytes = bytes(await tg_file.download_as_bytearray())
    except Exception as e:
        logger.error("Failed to download file '%s': %s", filename, e)
        await update.message.reply_text(
            f"Couldn't download that file, boss. Try sending it again."
        )
        return

    # Audio files: transcribe instead of text-extracting
    audio_exts = {".m4a", ".mp3", ".wav", ".ogg", ".oga", ".opus", ".flac", ".aac", ".wma"}
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""
    if f".{ext}" in audio_exts:
        transcript = await asyncio.to_thread(
            transcribe.transcribe, file_bytes, WHISPER_LANGUAGE
        )
        if not transcript:
            await update.message.reply_text(
                f"Couldn't transcribe '{filename}', boss. The audio might be corrupted or empty."
            )
            return

        logger.info("Audio file transcribed '%s' (%d chars): %s", filename, len(transcript), transcript[:100])

        caption = update.message.caption or ""
        visibility = _visibility_from_caption(caption)

        # Ingest transcript into vector memory
        result = await file_ingest.ingest_file(
            user_id, transcript.encode("utf-8"), f"{filename}.txt", visibility=visibility,
        )
        tag = " [SHARED with Slack]" if visibility == "shared" else ""
        status = (
            f"Transcribed '{filename}' — {len(transcript):,} characters."
            + (f" Indexed in {result['chunks']} chunks.{tag}" if result.get("success") else "")
        )
        await update.message.reply_text(status)
        await db.save_message(user_id, "user", f"[Audio file uploaded: {filename}]")
        await db.save_message(user_id, "assistant", status)

        message = (
            f"[Audio file '{filename}' transcribed ({len(transcript):,} chars), "
            f"indexed in memory.]\n\nTranscript:\n{transcript[:3000]}"
        )
        if caption:
            message += f"\n\nUser's note: {caption}"
        _update_contexts[update.update_id] = context
        await _message_queue.enqueue(user_id, message, update)
        return

    caption = update.message.caption or ""
    visibility = _visibility_from_caption(caption)

    # Persist to disk so the brain can reference the file by path (e.g. DemoATS).
    saved_path = await asyncio.to_thread(_save_upload, file_bytes, filename)

    # Run the ingest pipeline
    result = await file_ingest.ingest_file(user_id, file_bytes, filename, visibility=visibility)

    if result["success"]:
        tag = " — SHARED with Slack" if visibility == "shared" else ""
        msg = (
            f"Filed away, boss. '{filename}' — {result['chunks']} chunks, "
            f"{result['chars']:,} characters, all indexed in memory{tag}. "
            f"Ask me anything about it anytime."
        )
        await update.message.reply_text(msg)

        # Save to conversation so the brain knows about it (incl. local path)
        user_msg = f"[File uploaded: {filename} — saved at {saved_path}]"
        if caption:
            user_msg += f" Caption: {caption}"
        await db.save_message(user_id, "user", user_msg)
        await db.save_message(user_id, "assistant", msg)

        # If there's a caption, treat it as a follow-up message about the file
        if caption:
            _update_contexts[update.update_id] = context
            await _message_queue.enqueue(
                user_id,
                f"[I just uploaded a file called '{filename}' to your memory "
                f"(saved at {saved_path}). It has {result['chunks']} chunks indexed.] {caption}",
                update,
            )
    else:
        # Don't just reject — pass to brain so Saul can reason about the file
        caption = update.message.caption or ""
        ext = Path(filename).suffix.lower()
        _update_contexts[update.update_id] = context
        await _message_queue.enqueue(
            user_id,
            f"[The boss sent a file '{filename}' ({file_size:,} bytes, type: {ext}, "
            f"saved at {saved_path}). "
            f"Automatic text extraction failed: {result['error']}. "
            f"The file has been downloaded but couldn't be parsed automatically. "
            f"Think about what this file type is and how you might help — "
            f"suggest alternatives, offer to try different approaches, "
            f"or ask clarifying questions.]{' ' + caption if caption else ''}",
            update,
        )


@authorized
async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle photo messages — analyze with Gemini Vision and enqueue."""
    # De-duplicate
    update_id = update.update_id
    if update_id in _processed_updates:
        logger.debug("Skipping duplicate photo update %d", update_id)
        return
    _processed_updates.add(update_id)
    if len(_processed_updates) > _MAX_PROCESSED_IDS:
        to_remove = sorted(_processed_updates)[:_MAX_PROCESSED_IDS // 2]
        _processed_updates.difference_update(to_remove)

    user_id = str(update.effective_user.id)
    await update.message.chat.send_action("typing")

    # Download highest resolution photo
    photo = update.message.photo[-1]
    photo_file = await photo.get_file()
    photo_bytes = bytes(await photo_file.download_as_bytearray())

    # Analyze with Gemini
    analysis = await vision.analyze_image(photo_bytes)
    logger.info("Photo analyzed (%d bytes): %s", len(photo_bytes), analysis[:100])

    caption = update.message.caption or ""
    user_text = caption if caption else "[The user sent a photo]"
    message = (
        f"{user_text}\n\n"
        f"[PHOTO ANALYSIS (reference only — do NOT dump this back verbatim):\n{analysis}]\n\n"
        f"[Respond briefly and naturally. React to the photo like a person — "
        f"don't list everything you see. The user can ask follow-up questions about details.]"
    )

    _update_contexts[update.update_id] = context
    await _message_queue.enqueue(user_id, message, update)


@authorized
async def handle_video(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle video messages — analyze with Gemini Vision and enqueue."""
    # De-duplicate
    update_id = update.update_id
    if update_id in _processed_updates:
        logger.debug("Skipping duplicate video update %d", update_id)
        return
    _processed_updates.add(update_id)
    if len(_processed_updates) > _MAX_PROCESSED_IDS:
        to_remove = sorted(_processed_updates)[:_MAX_PROCESSED_IDS // 2]
        _processed_updates.difference_update(to_remove)

    user_id = str(update.effective_user.id)
    video = update.message.video

    # Guard: reject if too large for Telegram download
    if video.file_size and video.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "That video's too big to download, boss. Keep it under 20MB."
        )
        return

    await update.message.chat.send_action("typing")

    # Download video
    video_file = await video.get_file()
    video_bytes = bytes(await video_file.download_as_bytearray())

    mime_type = video.mime_type or "video/mp4"
    analysis = await vision.analyze_video(video_bytes, mime_type)
    logger.info("Video analyzed (%d bytes): %s", len(video_bytes), analysis[:100])

    caption = update.message.caption or ""
    user_text = caption if caption else "[The user sent a video]"
    message = (
        f"{user_text}\n\n"
        f"[VIDEO ANALYSIS (reference only — do NOT dump this back verbatim):\n{analysis}]\n\n"
        f"[Respond briefly and naturally. React to the video like a person — "
        f"don't list everything you see. The user can ask follow-up questions about details.]"
    )

    _update_contexts[update.update_id] = context
    await _message_queue.enqueue(user_id, message, update)


@authorized
async def handle_video_note(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle round video notes — analyze with Gemini Vision and enqueue."""
    # De-duplicate
    update_id = update.update_id
    if update_id in _processed_updates:
        logger.debug("Skipping duplicate video_note update %d", update_id)
        return
    _processed_updates.add(update_id)
    if len(_processed_updates) > _MAX_PROCESSED_IDS:
        to_remove = sorted(_processed_updates)[:_MAX_PROCESSED_IDS // 2]
        _processed_updates.difference_update(to_remove)

    user_id = str(update.effective_user.id)
    video_note = update.message.video_note

    # Guard: reject if too large
    if video_note.file_size and video_note.file_size > 20 * 1024 * 1024:
        await update.message.reply_text(
            "That round video's too big, boss. Keep it under 20MB."
        )
        return

    await update.message.chat.send_action("typing")

    # Download video note
    vn_file = await video_note.get_file()
    vn_bytes = bytes(await vn_file.download_as_bytearray())

    analysis = await vision.analyze_video(vn_bytes, "video/mp4")
    logger.info("Video note analyzed (%d bytes): %s", len(vn_bytes), analysis[:100])

    message = (
        f"[The user sent a round video message]\n\n"
        f"[VIDEO ANALYSIS (reference only — do NOT dump this back verbatim):\n{analysis}]\n\n"
        f"[Respond briefly and naturally. React to the video like a person — "
        f"don't list everything you see. The user can ask follow-up questions about details.]"
    )

    _update_contexts[update.update_id] = context
    await _message_queue.enqueue(user_id, message, update)


async def _handle_message_batch(user_id: str, batch: MessageBatch):
    """Process a message batch from the queue system.

    This replaces the old _process_debounced / _handle_user_message chain.
    The queue already holds the lock and sets processing flag.
    """
    update = batch.last_update
    message = batch.combined_text
    # Retrieve context from the storage dict (not from the frozen Update object)
    context = _update_contexts.pop(update.update_id, None)
    # Also clean up any stale contexts from older updates in this batch
    for msg in batch.messages[:-1]:
        _update_contexts.pop(msg.update.update_id, None)

    if batch.count > 1:
        logger.info("Processing batch of %d messages from user %s", batch.count, user_id)

    await _handle_user_message_inner(user_id, message, update, context)


async def _handle_user_message_inner(user_id: str, message: str, update: Update, context: ContextTypes.DEFAULT_TYPE | None):
    """Core message handler — send to think() with streaming, let it use tools directly."""

    # --- Reply edit-mode intercept ---
    if context and context.user_data.get("editing_reply_id"):
        reply_id = context.user_data.pop("editing_reply_id")
        pending = reply_flow.get_pending(reply_id)
        if pending:
            pending.draft_text = message
            preview = reply_flow.build_preview(pending)
            keyboard = reply_flow.build_keyboard(pending.id)
            await update.message.reply_text(
                f"Updated draft:\n\n{preview}",
                reply_markup=keyboard,
            )
            return
        else:
            await update.message.reply_text("That draft expired. Send a new reply request.")
            return

    # Ensure session is active (creates/rotates as needed)
    session_id = await session_mod.ensure_session(user_id)
    await session_mod.increment_session_messages(session_id)

    await db.save_message(user_id, "user", message)

    # Keep typing indicator alive during processing
    typing_flag = {"active": True}
    async def _keep_typing():
        while typing_flag["active"]:
            try:
                await update.message.chat.send_action("typing")
            except Exception:
                pass
            await asyncio.sleep(4)
    typing_task = asyncio.create_task(_keep_typing())

    try:
        # Check if voice response is explicitly requested
        _VOICE_TRIGGERS = ["send voice", "voice message", "respond with voice",
                           "voice response", "audio message", "send audio",
                           "respond with audio", "use audio", "use voice",
                           "голосом", "ответь голосом", "аудио сообщение",
                           "голосовое сообщение"]
        # Strip internal prefixes so transcribed voice messages don't auto-trigger
        _check_text = re.sub(r'^\[Voice message transcription\]:\s*', '', message, flags=re.IGNORECASE).lower()
        wants_voice = TTS_ENABLED and any(t in _check_text for t in _VOICE_TRIGGERS)

        if wants_voice:
            response = await brain.think(user_id, message)
            clean_response = brain._strip_markdown(response) if response else response
            await db.save_message(user_id, "assistant", clean_response or response)

            from goodman import tts
            audio = await asyncio.to_thread(tts.synthesize, (clean_response or response)[:3000])
            if audio:
                await update.message.reply_voice(voice=audio)
            else:
                await _send_message(update, clean_response or response)
            await _send_pending_files(update)
        else:
            # Stream filter suppresses [TG_REPLY:]/[WA_REPLY:] directive blocks
            # during streaming so the boss never sees raw tags.
            stream_filter = _StreamFilter(lambda text: _send_message(update, text))

            response = await brain.think(
                user_id, message, stream_callback=stream_filter,
            )
            await stream_filter.finish()

            clean_response = brain._strip_markdown(response) if response else response
            display_response = _strip_reply_directives(clean_response or response)
            await db.save_message(user_id, "assistant", display_response)

            if not stream_filter.sent_any:
                await _send_message(update, display_response)

            # Intercept reply directives → confirmation keyboard or direct send
            await _handle_reply_directives(response, update)

            # Send any files the brain queued via [SEND_FILE:] directives
            await _send_pending_files(update)

    except Exception as e:
        logger.error("Handler crashed: %s", e, exc_info=True)
        try:
            await _send_message(update, "Something went wrong. Try again.")
        except Exception:
            pass
    finally:
        typing_flag["active"] = False
        typing_task.cancel()


def _strip_reply_directives(text: str) -> str:
    """Remove [TG_REPLY:]...[/TG_REPLY] and [WA_REPLY:]...[/WA_REPLY] blocks from text."""
    text = _TG_REPLY_RE.sub("", text)
    text = _WA_REPLY_RE.sub("", text)
    return text.strip()


async def _handle_reply_directives(response: str, update: Update):
    """Parse brain response for TG/WA reply directives and show confirmation or send directly."""
    if not response:
        return

    for m in _TG_REPLY_RE.finditer(response):
        target = m.group(1)               # "tg_100000001" or "@demo_user1"
        display_name = m.group(2).strip()
        draft_text = m.group(3).strip()
        if not draft_text:
            continue

        if target.startswith("tg_"):
            chat_id = target.replace("tg_", "")
            db_user_id = target
        else:
            chat_id = target  # @username — Telethon resolves it
            db_user_id = None

        if db_user_id:
            recent = await db.get_recent_messages(db_user_id, limit=1)
            original = recent[-1]["content"] if recent else "(new conversation)"
        else:
            original = "(new contact — sending via username)"

        pending = reply_flow.PendingReply(
            platform="telegram_dm",
            target_chat_id=chat_id,
            target_name=display_name,
            original_message=original[:300],
            draft_text=draft_text,
        )
        reply_flow.store_pending(pending)
        preview = reply_flow.build_preview(pending)
        keyboard = reply_flow.build_keyboard(pending.id)
        await update.message.reply_text(preview, reply_markup=keyboard)
        logger.info("TG DM reply queued for %s (target=%s)", display_name, target)

    for m in _WA_REPLY_RE.finditer(response):
        phone = m.group(1)
        display_name = m.group(2).strip()
        draft_text = m.group(3).strip()
        if not draft_text:
            continue

        wa_user_id = f"wa_{phone}"
        recent = await db.get_recent_messages(wa_user_id, limit=1)
        original = recent[-1]["content"] if recent else "(no prior messages)"

        pending = reply_flow.PendingReply(
            platform="whatsapp",
            target_chat_id=phone,
            target_name=display_name,
            original_message=original[:300],
            draft_text=draft_text,
        )
        reply_flow.store_pending(pending)
        preview = reply_flow.build_preview(pending)
        keyboard = reply_flow.build_keyboard(pending.id)
        await update.message.reply_text(preview, reply_markup=keyboard)
        logger.info("WA reply queued for %s", display_name)


async def _send_to_telegram(text: str):
    """Send a notification to the boss's Telegram."""
    if _send_proactive:
        await _send_proactive(text)


_AUDIO_EXTENSIONS = {".m4a", ".mp3", ".ogg", ".oga", ".opus", ".wav", ".aac", ".webm", ".mp4"}
_AUDIO_MIMETYPES = {"audio/", "video/mp4"}


_IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".heic", ".tiff"}


def _is_audio_file(file_info: dict) -> bool:
    """Detect audio files by extension or mimetype."""
    name = (file_info.get("name") or "").lower()
    mime = (file_info.get("mimetype") or "").lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return ext in _AUDIO_EXTENSIONS or any(mime.startswith(m) for m in _AUDIO_MIMETYPES)


def _is_image_file(file_info: dict) -> bool:
    """Detect image files by extension or mimetype."""
    name = (file_info.get("name") or "").lower()
    mime = (file_info.get("mimetype") or "").lower()
    ext = "." + name.rsplit(".", 1)[-1] if "." in name else ""
    return ext in _IMAGE_EXTENSIONS or mime.startswith("image/")


async def _ingest_slack_files(files: list, slack_user_id: str) -> list:
    """Download Slack file attachments and ingest into shared vector memory.

    Audio files (voice messages, m4a, etc.) are transcribed via Whisper
    instead of text-ingested. Returns a list of human-readable status lines.
    """
    import httpx
    out = []
    headers = {"Authorization": f"Bearer {SLACK_BOT_TOKEN}"}
    timeout = httpx.Timeout(60.0, connect=15.0)
    async with httpx.AsyncClient(timeout=timeout) as http:
        for f in files:
            name = f.get("name") or "untitled"
            url = f.get("url_private_download")
            if not url:
                out.append(f"• {name}: no download URL")
                continue
            try:
                r = await http.get(url, headers=headers, follow_redirects=True)
                r.raise_for_status()
                file_bytes = r.content
            except Exception as e:
                logger.error("Slack file download failed for '%s': %s", name, e)
                out.append(f"• {name}: download failed ({e.__class__.__name__})")
                continue

            if _is_audio_file(f):
                ext = "." + name.rsplit(".", 1)[-1] if "." in name else ".m4a"
                transcript = await asyncio.to_thread(
                    transcribe.transcribe, file_bytes, "auto", ext,
                )
                if transcript:
                    out.append(f"• {name}: voice transcription ({len(transcript)} chars): {transcript}")
                else:
                    logger.warning("Slack audio transcription empty for '%s' (%d bytes)", name, len(file_bytes))
                    out.append(f"• {name}: audio file ({len(file_bytes):,} bytes) — transcription returned empty")
                continue

            if _is_image_file(f):
                mime = (f.get("mimetype") or "image/jpeg").lower()
                analysis = await vision.analyze_image(file_bytes, mime)
                logger.info("Slack image analyzed '%s' (%d bytes): %s", name, len(file_bytes), analysis[:100])
                out.append(f"• {name}: [IMAGE ANALYSIS — react naturally, do NOT dump verbatim]\n{analysis}")
                continue

            saved_path = await asyncio.to_thread(_save_upload, file_bytes, name)
            try:
                result = await file_ingest.ingest_file(
                    slack_user_id, file_bytes, name, visibility="shared",
                )
            except Exception as e:
                logger.error("Slack file ingest failed for '%s': %s", name, e, exc_info=True)
                out.append(f"• {name}: ingest crashed ({e.__class__.__name__}) — saved at {saved_path}")
                continue

            if result.get("success"):
                out.append(f"• {name}: {result['chunks']} chunks, {result['chars']:,} chars (shared) — saved at {saved_path}")
            else:
                out.append(f"• {name}: {result.get('error', 'unknown error')} — saved at {saved_path}")
    return out


async def _handle_slack_event(event):
    """Process an incoming Slack message through the brain pipeline.

    Handles both privileged users (full tool access) and regular users
    (conversation only). Sends response back to Slack and notifies
    Alex on Telegram.
    """
    from goodman import slack as slack_mod

    is_privileged = event.user_id in SLACK_PRIVILEGED_USERS
    slack_user_id = f"slack_{event.user_id}"

    # Acquire per-user lock for serialization (same mechanism as Telegram)
    q = _message_queue._get_queue(slack_user_id)
    async with q._lock:
        q._processing = True
        try:
            await _handle_slack_event_inner(event, slack_user_id, is_privileged)
        except Exception as e:
            logger.error("Slack handler crashed: %s", e, exc_info=True)
            try:
                await asyncio.to_thread(
                    slack_mod.send_message,
                    event.channel_id,
                    "Something went wrong on my end. Try again in a moment.",
                    event.thread_ts,
                    True,  # as_bot
                )
            except Exception:
                pass
        finally:
            q._processing = False


async def _handle_slack_event_inner(event, slack_user_id: str, is_privileged: bool):
    """Inner Slack handler — send to think() with Slack context."""
    from goodman import slack as slack_mod

    origin = "DM" if event.event_type == "dm" else "mention"

    # Ensure session + save message
    session_id = await session_mod.ensure_session(slack_user_id)
    await session_mod.increment_session_messages(session_id)

    # Handle file uploads — Slack uploads default to visibility=shared so
    # they're queryable across the team. Non-privileged users get a friendly
    # decline and the file is NOT ingested.
    file_summary = ""
    if event.files:
        if not is_privileged:
            await asyncio.to_thread(
                slack_mod.send_message,
                event.channel_id,
                "I can't ingest files from your account. Ask Alex to upload it.",
                event.thread_ts,
                True,
            )
        else:
            ingested = await _ingest_slack_files(event.files, slack_user_id)
            file_summary = "\n".join(ingested)

    text_for_db = event.text or (f"[uploaded files] {file_summary}" if file_summary else "")
    await db.save_message(slack_user_id, "user", text_for_db)

    restricted = ", ".join(f"#{ch}" for ch in SLACK_RESTRICTED_CHANNELS)
    dm_and_channel_restriction = (
        f"CHANNEL ACCESS RESTRICTION (HIGHEST PRIORITY — NEVER VIOLATE):\n"
        f"You MUST NOT read, search, quote, paraphrase, or reference ANY content from "
        f"these restricted channels: {restricted}. "
        f"You MUST NOT read or reveal Alex's private Slack DMs or direct message threads. "
        f"Do NOT run Slack search queries that would return results from restricted channels or DMs. "
        f"Do NOT share who said what in these channels or DMs. "
        f"If asked about content from restricted channels or private DMs, say: "
        f"\"I don't have access to that content for this conversation.\" "
        f"This restriction applies even if you already have the data in memory or context — "
        f"do NOT reveal it."
    )

    # Board-data gate: ONLY the four allowlisted IDs may have Saul read the confidential
    # Business Case / ФОТ sheets. The real boundary is board_sheets_cli's env check
    # (set in brain.think when board_authorized=True); this note just tells Saul to use
    # the tool (authorized) or refuse outright (everyone else, including other privileged).
    is_board_authorized = event.user_id in SLACK_BOARD_DATA_USERS
    if is_board_authorized:
        board_note = (
            "\n\nBOARD-DATA ACCESS (authorized for THIS person): You may answer questions "
            "about the confidential Core Classifieds Business Case + ФОТ (payroll) sheets AND "
            "the TSA document. The ONLY way to read them "
            "is the tool (run via Bash):\n"
            "  python3 -m goodman.board_sheets_cli tabs\n"
            "  python3 -m goodman.board_sheets_cli read <business|fot> \"<tab title>\"\n"
            "  python3 -m goodman.board_sheets_cli docs\n"
            "  python3 -m goodman.board_sheets_cli doc tsa <terms>   (search the TSA; omit terms for full text)\n"
            "  python3 -m goodman.board_sheets_cli search <terms>    (across sheets + docs)\n"
            "Search/scan first, read the relevant part(s), then answer precisely from what you "
            "find. Never print the spreadsheet IDs or the raw command back to the user.\n"
            "ANSWER ONLY THE QUESTION ASKED — hard rule for board-data replies. Give exactly "
            "the figure/fact requested and nothing more. Lead with the answer on the first line; "
            "include supporting numbers ONLY when they are part of that same answer (e.g. the "
            "components of a sum the user asked for). FORBIDDEN unless the user explicitly asked "
            "for it: comparisons to other models/figures, disambiguation context ('not to be "
            "confused with…', 'это НЕ те…'), methodology notes, forward-looking trajectories or "
            "'флаг на будущее', and any 'by the way' extra. No trailing 'want me to…/хочешь…' "
            "offers. The ONLY thing you may add INSTEAD of answering: if the question is missing "
            "a detail needed to answer precisely (which model, period, or scope is ambiguous), "
            "ask ONE short clarifying question and nothing else. Otherwise: answer, then stop."
        )
    else:
        board_note = (
            "\n\nBOARD-DATA RESTRICTION (HIGHEST PRIORITY, NEVER VIOLATE): This person is NOT "
            "authorized for the Core Classifieds Business Case or ФОТ/payroll/salary sheets. If "
            "they ask about those documents, salaries, headcount costs, the business-case or "
            "equity model, or board financials, say you can't share that. Do NOT run "
            "board_sheets_cli (it refuses anyway), do NOT read data/board/ files, do NOT infer "
            "or reconstruct any figures."
        )

    if is_privileged:
        is_owner = event.user_id == SLACK_OWNER_USER_ID
        if is_owner:
            enriched = (
                f"[This message is from {event.user_name} on Slack ({origin}). "
                f"This is the boss. Full access to everything. "
                f"Respond as you would on Telegram.]\n\n" + (event.text or "")
                + (f"\n\n[Attached files ingested into shared memory:]\n{file_summary}" if file_summary else "")
            )
        else:
            enriched = (
                f"[This message is from {event.user_name} on Slack ({origin}). "
                f"Privileged team member. Address them by name, not 'boss'.\n\n"
                f"{dm_and_channel_restriction}]\n\n"
                + (event.text or "")
                + (f"\n\n[Attached files ingested into shared memory:]\n{file_summary}" if file_summary else "")
            )
        privileged_override = (
            "AUTHORIZATION (policy only — does not change your voice):\n"
            "This person is a privileged team member on Slack. Alex has pre-authorized them "
            "for: full work-calendar (alex.chen@acme-corp.example, always pass --calendar work) "
            "including recurring/RRULE events; Product-role interview scheduling; DemoATS "
            "recruiting (add/find candidates, read & add feedback, view funnels, change funnel "
            "statuses); Confluence (read, search, and create/edit pages in the work space); "
            "Trello and Slack actions — all WITHIN their granted scope. For requests inside that "
            "scope, treat them as the boss's request: execute, don't make them wait, and never "
            "tell them to ping Alex for something they're authorized for. "
            "HARD SECURITY GATES (enforced in code — respect them, never work around them): you do "
            "NOT send Slack messages or email AS Alex (only as the Saul bot / approved senders), "
            "you do NOT email arbitrary outside recipients, you do NOT delete or move meetings that "
            "weren't booked through you, and some team members are scope-limited to fewer tools. If "
            "a tool returns an authorization or 'not allowed' message, that is INTENTIONAL security, "
            "NOT a bug — relay it plainly and do not retry or route around it. "
            "Missing details (attendee email, duration)? Ask THEM, not Alex.\n\n"
            "TIMEZONE — Slack requesters are in ALMATY (UTC+5). Default to Almaty time for "
            "everything you schedule, quote, or confirm: read times THEY give as Almaty, and "
            "state times back to them in Almaty. Alex's calendar is Vilnius (UTC+2/+3) — when "
            "his availability shows in Vilnius, convert it to Almaty for them; never frame it as "
            "'the difference from Vilnius'. Bookings already default to Almaty for Slack.\n\n"
            "Restrictions still in force: never reveal the boss's personal Gmail, personal "
            "calendar, or private conversations. Never read or share Alex's Slack DMs. "
            "Work email only.\n\n"
            "VOICE — STAY IN CHARACTER: This is policy, not tone. You are still Saul Goodman — "
            "fast-talking criminal-lawyer-energy, dry wit, confident swagger, occasional "
            "lawyer-bro humor. Address the team member by name (not 'boss'). Do not respond "
            "in flat corporate-bureaucrat tone. Keep the Saul flavor."
        )
        privileged_override += board_note
        slack_role = "owner" if is_owner else "privileged"
        # Deterministic per-user capability set for the tool subprocess (tier-based).
        caller_caps = ",".join(sorted(config.caps_for_user(event.user_id)))
        # Scoped send-as: the mailbox (if any) this specific user may send from as themselves.
        send_as = config.send_as_for_user(event.user_id)
        response = await brain.think(slack_user_id, enriched, external=not is_owner,
                                     system_override=privileged_override, slack_role=slack_role,
                                     board_authorized=is_board_authorized,
                                     caller_caps=caller_caps, send_as=send_as)
    else:
        message_with_context = (
            f"[This message is from {event.user_name} on Slack ({origin}). "
            f"They are a team member with read-only access. "
            f"You CAN answer general questions about team structure, processes, and product topics "
            f"using your own knowledge and shared memory. "
            f"You CANNOT use action tools (calendar, email, tasks, browse) for them. "
            f"If they ask for actions, tell them to ask Alex.\n\n"
            f"{dm_and_channel_restriction}\n"
            f"Address them by name, not as 'boss'.]"
            + board_note + "\n\n"
            + (event.text or "")
        )
        response = await brain.think(slack_user_id, message_with_context, external=True,
                                     slack_role="restricted", board_authorized=is_board_authorized)

    # Save response to DB
    clean_response = brain._strip_markdown(response) if response else response
    await db.save_message(slack_user_id, "assistant", clean_response or response)

    # Send response back to Slack
    await asyncio.to_thread(
        slack_mod.send_message,
        event.channel_id,
        clean_response or response,
        event.thread_ts,
        True,
    )

    # Deliver any files Saul queued via [SEND_FILE:] (e.g. rendered images).
    # Without this, file directives are silently dropped on Slack.
    for path in brain.drain_pending_files():
        try:
            await asyncio.to_thread(
                slack_mod.send_file, event.channel_id, path, "", "", event.thread_ts
            )
            logger.info("Sent Slack file: %s", path)
        except Exception as e:
            logger.error("Failed to send Slack file %s: %s", path, e)

    # Slack interactions are saved under their own user IDs (slack_*).
    # No longer saved to boss's conversation — prevents notification flood
    # from consuming the 200-message context window.


# --- Telegram Userbot DM event handler ---

async def _handle_tg_dm_event(event):
    """Handle an incoming Telegram DM from the userbot listener.

    Saves to the sender's own conversation (tg_{id}) for later querying.
    Does NOT save to boss's conversation — this prevents notification flood
    from consuming the 200-message context window and drowning real conversation.
    The brain can access DM history via convsearch or get_recent_dms when needed.
    """
    tg_user_id = f"tg_{event.sender_id}"

    # Save with sender name prefix so reply flow can extract the name later
    username_str = f" (@{event.sender_username})" if event.sender_username else ""
    display_name = f"{event.sender_name}{username_str}"
    db_content = f"[From: {display_name}] {event.text}"
    await db.save_message(tg_user_id, "user", db_content)


# --- Telegram group event handler ---

async def _handle_tg_group_event(event):
    """Handle an incoming Telegram group message from the userbot listener.

    Always saves to DB for later querying. Only sends notification to the boss
    if the message matches the keyword/mention filter.
    """
    group_user_id = f"tg_group_{event.group_id}"

    # Save with sender name + group prefix (matches WA pattern)
    username_str = f" (@{event.sender_username})" if event.sender_username else ""
    display_name = f"{event.sender_name}{username_str}"
    db_content = f"[From: {display_name} in {event.group_name}] {event.text}"
    await db.save_message(group_user_id, "user", db_content)

    # Check notification filter — only send proactive alert for keyword matches.
    # Group messages are already saved under tg_group_* user IDs above.
    # NOT saved to boss's conversation — prevents context window flooding.
    if _tg_listener:
        trigger = _tg_listener.matches_notification_filter(event.text)
        if trigger and _send_proactive:
            short = event.text[:200]
            await _send_proactive(
                f"[TG Group: {event.group_name}] {display_name} ({trigger}):\n{short}"
            )


# --- WhatsApp event handler ---

async def _handle_wa_event(event):
    """Handle an incoming WhatsApp message from the sidecar listener.

    Saves to DB and sends notification to the boss's Telegram bot chat.
    Does NOT auto-respond — the user must explicitly ask Saul to reply.
    """
    wa_user_id = f"wa_{event.sender_phone}"

    # Save with sender name prefix so reply flow can extract the name later
    group_str = f" in {event.group_name}" if event.is_group and event.group_name else ""
    display_name = f"{event.sender_name} ({event.sender_phone})"
    db_content = f"[From: {display_name}{group_str}] {event.text}"
    await db.save_message(wa_user_id, "user", db_content)

    # WhatsApp messages saved under wa_* user IDs above.
    # NOT saved to boss's conversation — prevents context window flooding.


# --- Reply confirmation callback handler ---

async def handle_reply_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks for cross-platform reply confirmation."""
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    if TELEGRAM_ALLOWED_USER_ID and user_id != TELEGRAM_ALLOWED_USER_ID:
        await query.edit_message_text("Nice try, pal. Wrong number.")
        return

    # Parse callback data: reply_approve:XXXXXXXX, reply_edit:XXXXXXXX, reply_cancel:XXXXXXXX
    parts = query.data.split(":", 1)
    if len(parts) != 2:
        await query.edit_message_text("Invalid callback data.")
        return

    action, reply_id = parts[0].replace("reply_", ""), parts[1]
    pending = reply_flow.get_pending(reply_id)

    if not pending:
        await query.edit_message_text("That draft expired. Send a new reply request, counselor.")
        return

    if action == "cancel":
        reply_flow.discard_pending(reply_id)
        await query.edit_message_text("Scrapped. Consider it shredded, boss.")
        await db.save_message(user_id, "assistant",
                              f"Reply to {pending.target_name} cancelled by user.")
        return

    if action == "edit":
        # Store the reply ID and wait for the next text message to replace the draft
        context.user_data["editing_reply_id"] = reply_id
        await query.edit_message_text(
            f"Send me the updated reply text. I'll re-draft it.\n\n"
            f"(Replying to {pending.target_name})"
        )
        return

    if action == "approve":
        reply_flow.discard_pending(reply_id)

        # Send via the appropriate platform listener
        try:
            if pending.platform == "telegram_dm" and _tg_listener:
                # target_chat_id can be numeric ("12345") or username ("@dovatwil")
                # Telethon accepts both — only convert to int if it's numeric
                target = pending.target_chat_id
                if target.startswith("@"):
                    chat_target = target  # username — Telethon resolves it
                else:
                    chat_target = int(target.replace("tg_", ""))
                await _tg_listener.send_message_async(chat_target, pending.draft_text)
                # Save our reply to DB
                tg_user_id = f"tg_{pending.target_chat_id}"
                await db.save_message(tg_user_id, "assistant", pending.draft_text)
                await query.edit_message_text(
                    f"Sent via Telegram DM to {pending.target_name}:\n{pending.draft_text[:300]}"
                )
                await db.save_message(user_id, "assistant",
                                      f"Sent TG DM reply to {pending.target_name}")

            elif pending.platform == "whatsapp" and _wa_listener:
                _wa_listener.send_message(pending.target_chat_id, pending.draft_text)
                wa_user_id = f"wa_{pending.target_chat_id}"
                await db.save_message(wa_user_id, "assistant", pending.draft_text)
                await query.edit_message_text(
                    f"Sent via WhatsApp to {pending.target_name}:\n{pending.draft_text[:300]}"
                )
                await db.save_message(user_id, "assistant",
                                      f"Sent WhatsApp reply to {pending.target_name}")

            else:
                await query.edit_message_text(
                    f"Can't send — {pending.platform} listener isn't running."
                )
        except Exception as e:
            logger.error("Failed to send reply: %s", e, exc_info=True)
            await query.edit_message_text(f"Failed to send: {e}")
        return

    await query.edit_message_text("Unknown action. Something went sideways.")


async def handle_email_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks for email send/reply confirmation."""
    query = update.callback_query
    await query.answer()

    user_id = str(update.effective_user.id)
    if TELEGRAM_ALLOWED_USER_ID and user_id != TELEGRAM_ALLOWED_USER_ID:
        await query.edit_message_text("Nice try, pal. Wrong number.")
        return

    pending = context.user_data.get("pending_email")
    if not pending:
        await query.edit_message_text("Nothing pending, counselor. That ship has sailed.")
        return

    if query.data == "email_cancel":
        context.user_data.pop("pending_email", None)
        await query.edit_message_text("Scrapped. Consider it shredded, boss.")
        await db.save_message(user_id, "assistant", "Email cancelled by user.")
        return

    if query.data == "email_confirm":
        action = pending.get("action")
        context.user_data.pop("pending_email", None)

        if action == "send":
            result = await asyncio.to_thread(
                gmail.send_email,
                pending["to"],
                pending["subject"],
                pending["body"],
                pending.get("from_account"),
            )
            await query.edit_message_text(f"Done. {result}")
            await db.save_message(user_id, "assistant", f"Email sent: {result}")

        elif action == "reply":
            if not pending.get("message_id"):
                await query.edit_message_text(
                    "Can't send — no pinned Message-ID. Try the reply again.")
                return
            result = await asyncio.to_thread(
                gmail.reply_by_message_id,
                pending["message_id"],
                pending["reply_body"],
            )
            await query.edit_message_text(f"Done. {result}")
            await db.save_message(user_id, "assistant", f"Reply sent: {result}")

        else:
            await query.edit_message_text("Something went sideways. Unknown action.")


async def _missed_messages_digest():
    """Periodic background task: send a digest of missed cross-platform messages."""
    global _digest_since
    await asyncio.sleep(120)  # Let bot fully initialize
    tz = ZoneInfo(TIMEZONE)
    _digest_since = datetime.now(tz).isoformat()

    while True:
        await asyncio.sleep(_DIGEST_INTERVAL)
        try:
            now = datetime.now(tz)
            # Skip quiet hours (don't wake the boss)
            if now.hour >= _DIGEST_QUIET_START or now.hour < _DIGEST_QUIET_END:
                continue

            messages = await db.get_cross_platform_messages_since(_digest_since)
            _digest_since = now.isoformat()

            if not messages:
                continue

            # Build digest grouped by source
            lines = []
            for msg in messages[-15:]:  # Cap at 15 most recent
                uid = msg["user_id"]
                source = uid.split("_")[0].upper()
                content = msg["content"]
                # Strip the "[From: Name]" prefix if present, keep it readable
                lines.append(f"[{source}] {content[:250]}")

            header = f"You have {len(messages)} missed message(s):"
            if len(messages) > 15:
                header += f" (showing latest 15 of {len(messages)})"
            digest = header + "\n\n" + "\n\n".join(lines)
            await _send_to_telegram(digest)
            logger.info("Sent missed-message digest: %d messages", len(messages))
        except Exception as e:
            logger.error("Missed-message digest error: %s", e)


async def post_init(application: Application):
    """Run after bot initialization."""
    global _heartbeat, _scheduler, _send_proactive, _tg_listener, _wa_listener

    await db.init_db()
    await session_mod.init_session_db()
    from goodman import knowledge_graph as kg
    await kg.init_db()
    from goodman import learning
    await learning.init_db()
    soul.ensure_dirs()
    soul.cleanup_old_logs()

    # Wire the message queue batch handler
    _message_queue.set_handler(_handle_message_batch)

    # Set up proactive Telegram delivery callback
    if TELEGRAM_ALLOWED_USER_ID:
        async def send_proactive(text: str):
            """Send a proactive message to the boss via Telegram."""
            try:
                bot = application.bot
                while text:
                    chunk = text[:TELEGRAM_MAX_LENGTH]
                    await bot.send_message(chat_id=int(TELEGRAM_ALLOWED_USER_ID), text=chunk)
                    text = text[TELEGRAM_MAX_LENGTH:]
            except Exception as e:
                logger.error("Failed to send proactive message: %s", e)

        _send_proactive = send_proactive

        _heartbeat = Heartbeat(send_callback=send_proactive)
        _heartbeat.start()
        logger.info("Heartbeat system armed and ready.")

        _scheduler = Scheduler(send_callback=send_proactive)
        _scheduler.start()
        logger.info("Scheduler system online.")

        if CONSULATE_MONITOR_ENABLED:
            global _consulate_monitor
            from goodman.consulate_monitor import ConsulateMonitor
            _consulate_monitor = ConsulateMonitor(send_callback=send_proactive)
            _consulate_monitor.start()
            logger.info("Consulate monitor armed — checking slots periodically.")

        # Start missed-message digest (hourly summary instead of instant spam)
        asyncio.create_task(_missed_messages_digest())
        logger.info("Missed-message digest armed (every %ds).", _DIGEST_INTERVAL)

        if SLACK_BOT_TOKEN and SLACK_APP_TOKEN:
            global _slack_listener
            from goodman.slack_listener import SlackListener
            _slack_listener = SlackListener(
                send_callback=send_proactive,
                message_callback=_handle_slack_event,
            )
            _slack_listener.start()
            logger.info("Slack listener connected.")

        if TELETHON_API_ID and TELETHON_API_HASH:
            from goodman.tg_listener import TelegramUserbotListener
            _tg_listener = TelegramUserbotListener(
                send_callback=send_proactive,
                event_callback=_handle_tg_dm_event,
                group_event_callback=_handle_tg_group_event,
            )
            _tg_listener.start()
            logger.info("Telethon userbot listener starting.")

        if WA_ENABLED:
            async def send_photo(photo_bytes: bytes, caption: str):
                """Send a photo to the boss via Telegram."""
                try:
                    import io
                    bot = application.bot
                    await bot.send_photo(
                        chat_id=int(TELEGRAM_ALLOWED_USER_ID),
                        photo=io.BytesIO(photo_bytes),
                        caption=caption,
                    )
                except Exception as e:
                    logger.error("Failed to send photo: %s", e)

            from goodman.wa_listener import WhatsAppListener
            _wa_listener = WhatsAppListener(
                send_callback=send_proactive,
                event_callback=_handle_wa_event,
                send_photo_callback=send_photo,
            )
            _wa_listener.start()
            logger.info("WhatsApp listener starting.")

        # Start HTTP API server (for VisionClaw / external integrations)
        from goodman import api
        await api.start()

    # Pre-warm embedding model in background (eliminates ~3s cold start on first message)
    async def _prewarm_embeddings():
        try:
            from goodman.embeddings import _get_model
            await asyncio.get_event_loop().run_in_executor(None, _get_model)
            logger.info("Embedding model pre-warmed.")
        except Exception as e:
            logger.warning("Embedding pre-warm failed (will lazy-load): %s", e)
    asyncio.create_task(_prewarm_embeddings())

    logger.info("Database initialized. Soul loaded. Saul Goodman is open for business.")


# --- Content-engine relay (Saul is the courier for the post pipeline) ---
# Fully guarded: if the engine can't import, these become harmless no-ops and Saul
# is unaffected.
try:
    import os as _os
    import sys as _sys
    _CE_DIR = _os.path.expanduser("~/content_engine")
    if _CE_DIR not in _sys.path:
        _sys.path.insert(0, _CE_DIR)
    from engine import relay as _ce_relay
    from telegram.ext import ApplicationHandlerStop as _CEStop
    _CE_OK = True
except Exception as _ce_err:  # pragma: no cover
    _ce_relay = None
    _CE_OK = False


async def handle_ce_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Relay '^ce_' inline-button taps to the content engine."""
    query = update.callback_query
    await query.answer()
    if not _CE_OK:
        return
    user_id = str(update.effective_user.id)
    if TELEGRAM_ALLOWED_USER_ID and user_id != TELEGRAM_ALLOWED_USER_ID:
        return
    try:
        await _ce_relay.handle_callback(query.data, query.message.chat_id)
    except Exception as e:
        logger.warning("CE callback error: %s", e)


async def handle_dsr_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Design-system component request Approve/Reject buttons (dsr_ok_N / dsr_no_N)."""
    query = update.callback_query
    await query.answer()
    user_id = str(update.effective_user.id)
    if TELEGRAM_ALLOWED_USER_ID and user_id != TELEGRAM_ALLOWED_USER_ID:
        return
    try:
        from goodman import dsreq
        action, rid = query.data.rsplit("_", 1)
        loop = asyncio.get_running_loop()
        outcome = await loop.run_in_executor(
            None, dsreq.decide, int(rid), action == "dsr_ok")
        await query.edit_message_text(
            f"{query.message.text}\n\n— {outcome}")
    except Exception as e:
        logger.warning("dsreq callback error: %s", e)
        try:
            await query.message.reply_text(f"⚠️ dsreq error: {e}")
        except Exception:
            pass


async def handle_ce_feedback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Group -1: consume a text message ONLY when a CE iteration is pending for this
    chat; otherwise return and let Saul's normal handler process it."""
    if not _CE_OK or not update.message:
        return
    chat_id = update.effective_chat.id
    if not _ce_relay.feedback_pending(chat_id):
        return  # not ours — Saul handles it normally
    try:
        if await _ce_relay.handle_feedback(update.message.text, chat_id):
            raise _CEStop  # stop Saul from also answering this message
    except _CEStop:
        raise
    except Exception as e:
        logger.warning("CE feedback error: %s", e)


def main():
    """Start the bot."""
    if not TELEGRAM_BOT_TOKEN:
        print(
            "ERROR: No TELEGRAM_BOT_TOKEN found.\n"
            "Copy .env.example to .env and add your bot token from @BotFather."
        )
        return

    # Kill any previous instance and claim the PID lock
    _acquire_pid_lock()

    app = Application.builder().token(TELEGRAM_BOT_TOKEN).post_init(post_init).build()

    # Command handlers
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("brief", cmd_brief))
    app.add_handler(CommandHandler("calendar", cmd_calendar))
    app.add_handler(CommandHandler("email", cmd_email))
    app.add_handler(CommandHandler("code", cmd_code))
    app.add_handler(CommandHandler("research", cmd_research))
    app.add_handler(CommandHandler("soul", cmd_soul))
    app.add_handler(CommandHandler("heartbeat", cmd_heartbeat))
    app.add_handler(CommandHandler("jobs", cmd_jobs))
    app.add_handler(CommandHandler("tasks", cmd_tasks))
    app.add_handler(CommandHandler("consulate", cmd_consulate))
    app.add_handler(CommandHandler("cancel", cmd_cancel_job))
    app.add_handler(CommandHandler("reset", cmd_reset))
    app.add_handler(CommandHandler("slack", cmd_slack))

    # Callback handlers for inline buttons
    app.add_handler(CallbackQueryHandler(handle_reply_callback, pattern="^reply_"))
    app.add_handler(CallbackQueryHandler(handle_email_callback, pattern="^email_"))
    # Content-engine post-approval buttons (isolated by the ^ce_ pattern)
    app.add_handler(CallbackQueryHandler(handle_ce_callback, pattern="^ce_"))
    # Design-system component-request approval buttons
    app.add_handler(CallbackQueryHandler(handle_dsr_callback, pattern="^dsr_"))
    # Content-engine iteration feedback — runs in group -1, only consumes when a CE
    # iteration is pending (otherwise passes through to Saul's normal text handler).
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_ce_feedback), group=-1)

    # Voice handler (before text catch-all)
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))

    # Audio handler (m4a, mp3 etc. sent as audio — NOT voice messages)
    app.add_handler(MessageHandler(filters.AUDIO, handle_audio))

    # Document/file handler (before text catch-all)
    app.add_handler(MessageHandler(filters.Document.ALL, handle_document))

    # Photo/video handlers (before text catch-all)
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_note))

    # Message handler (catch-all for text)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))

    logger.info("It's showtime, folks! Saul Goodman is live.")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
