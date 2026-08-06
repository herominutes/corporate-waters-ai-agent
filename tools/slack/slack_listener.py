"""Slack Socket Mode listener — two-way messaging.

Real-time event listener using Slack's Socket Mode (WebSocket).
Uses the synchronous Bolt app in a background thread to avoid
event loop conflicts with the main asyncio bot.

DMs and @mentions are:
1. Processed through the brain pipeline (message_callback)
2. Forwarded to Telegram for visibility (send_callback)
"""

import asyncio
import logging
import re
import threading
from dataclasses import dataclass
from goodman.config import SLACK_BOT_TOKEN, SLACK_APP_TOKEN
from goodman import slack as slack_mod
from goodman import confluence

logger = logging.getLogger(__name__)


@dataclass
class SlackEvent:
    """Structured Slack event for the brain pipeline."""
    channel_id: str
    user_id: str
    user_name: str
    text: str
    thread_ts: str | None  # reply in thread if present
    event_type: str  # "dm" or "mention"
    files: list | None = None  # [{"id","name","url_private_download","mimetype","size"}]


def _enrich_confluence_links(text: str) -> str:
    """Detect Confluence URLs in text, fetch pages, append content + space context for relevance check."""
    if not confluence.enabled():
        return text
    try:
        pages = confluence.ingest_links_from_text(text)
        if not pages:
            return text
        parts = [text, ""]
        structure = confluence.get_space_structure_summary()
        parts.append(f"[CONFLUENCE CONTEXT — evaluate relevance before accepting]")
        parts.append(structure)
        parts.append("")
        for p in pages:
            body = p.get("body", "")
            parts.append(f"[Confluence page: {p['title']}]")
            parts.append(f"URL: {p['url']}")
            if body:
                parts.append(body[:8000])
            else:
                parts.append("(empty page)")
            parts.append("")
        return "\n".join(parts)
    except Exception as e:
        logger.error("Confluence enrichment error: %s", e)
    return text


class SlackListener:
    """Listen for Slack DMs and @mentions, process via brain and notify Telegram."""

    def __init__(self, send_callback, message_callback):
        """
        Args:
            send_callback: async callable(text: str) that sends to Telegram.
            message_callback: async callable(SlackEvent) that processes through brain.
        """
        self._send_callback = send_callback
        self._message_callback = message_callback
        self._loop = None  # will be set to the main event loop
        self.running = False
        self._thread = None
        self._handler = None

    def start(self):
        """Start the listener in a background thread."""
        if self.running:
            return
        if not SLACK_BOT_TOKEN or not SLACK_APP_TOKEN:
            logger.info("Slack listener: missing tokens, not starting")
            return
        self._loop = asyncio.get_event_loop()
        self.running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        logger.info("Slack listener starting...")

    def stop(self):
        """Stop the listener."""
        self.running = False
        if self._handler:
            try:
                self._handler.close()
            except Exception:
                pass
        logger.info("Slack listener stopped")

    def _send_sync(self, text: str):
        """Thread-safe bridge to the async send callback."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._send_callback(text), self._loop)

    def _process_sync(self, event: SlackEvent):
        """Thread-safe bridge to the async message callback."""
        if self._loop and self._loop.is_running():
            asyncio.run_coroutine_threadsafe(self._message_callback(event), self._loop)

    def _run(self):
        """Background thread: create Bolt app and connect via Socket Mode."""
        while self.running:
            try:
                self._connect()
            except Exception as e:
                logger.error("Slack listener error: %s", e)
                if self.running:
                    logger.info("Slack listener: reconnecting in 30s...")
                    import time
                    time.sleep(30)

    def _connect(self):
        """Establish Socket Mode connection and listen for events."""
        from slack_bolt import App
        from slack_bolt.adapter.socket_mode import SocketModeHandler

        app = App(token=SLACK_BOT_TOKEN)

        @app.event("app_mention")
        def on_mention(event, say):
            try:
                user_id = event.get("user", "")
                channel_id = event.get("channel", "")
                text = event.get("text", "")
                thread_ts = event.get("thread_ts") or event.get("ts")

                # Strip the bot mention from text (e.g. "<@U12345> schedule a meeting")
                bot_uid = slack_mod._get_bot_user_id()
                if bot_uid:
                    text = re.sub(rf"<@{bot_uid}>\s*", "", text).strip()

                user_name = slack_mod.resolve_user(user_id)

                text = _enrich_confluence_links(text)

                slack_event = SlackEvent(
                    channel_id=channel_id,
                    user_id=user_id,
                    user_name=user_name,
                    text=text,
                    thread_ts=thread_ts,
                    event_type="mention",
                )
                self._process_sync(slack_event)
            except Exception as e:
                logger.error("Slack on_mention error: %s", e)

        @app.event("message")
        def on_message(event):
            try:
                # Only handle DMs (channel_type=im), ignore bot's own messages
                if event.get("channel_type") != "im":
                    return
                if event.get("bot_id"):
                    return

                # Allow plain messages and file_share. Drop other subtypes
                # (channel_join, message_changed, etc.).
                subtype = event.get("subtype")
                if subtype and subtype != "file_share":
                    return

                user_id = event.get("user", "")
                text = event.get("text", "")
                channel_id = event.get("channel", "")
                thread_ts = event.get("thread_ts")

                # Skip if it's the bot's own user ID
                bot_uid = slack_mod._get_bot_user_id()
                if user_id == bot_uid:
                    return

                user_name = slack_mod.resolve_user(user_id)

                # Collect file objects from anywhere they may live:
                # - event["files"]: direct upload (file_share)
                # - event["attachments"][*]["files"]: forwarded message containing a file
                # - event["message"]["files"]: nested message wrap (rare in DMs)
                raw_files: list = []
                raw_files.extend(event.get("files") or [])
                for att in (event.get("attachments") or []):
                    raw_files.extend(att.get("files") or [])
                msg = event.get("message") or {}
                raw_files.extend(msg.get("files") or [])

                # Forwarded Slack messages also carry the original text inside
                # attachments[*]["text"] — fold it into the visible text so Saul
                # sees what was forwarded even if there's no file.
                forwarded_chunks: list = []
                for att in (event.get("attachments") or []):
                    fwd_text = att.get("text") or ""
                    if fwd_text and fwd_text not in (text or ""):
                        author = att.get("author_name") or att.get("service_name") or ""
                        forwarded_chunks.append(
                            f"[Forwarded from {author}]\n{fwd_text}" if author else fwd_text
                        )
                if forwarded_chunks:
                    text = (text + "\n\n" + "\n\n".join(forwarded_chunks)).strip()

                file_list = [
                    {
                        "id": f.get("id"),
                        "name": f.get("name") or f.get("title") or "untitled",
                        "url_private_download": f.get("url_private_download") or f.get("url_private"),
                        "mimetype": f.get("mimetype", ""),
                        "size": f.get("size", 0),
                    }
                    for f in raw_files
                    if f.get("url_private_download") or f.get("url_private")
                ] or None

                if (event.get("attachments") or msg.get("files")) and not file_list:
                    logger.info(
                        "Slack DM had attachments but no extractable file. "
                        "subtype=%s top_keys=%s att_keys=%s",
                        subtype,
                        list(event.keys()),
                        [list(a.keys()) for a in (event.get("attachments") or [])],
                    )

                text = _enrich_confluence_links(text)

                slack_event = SlackEvent(
                    channel_id=channel_id,
                    user_id=user_id,
                    user_name=user_name,
                    text=text,
                    thread_ts=thread_ts,
                    event_type="dm",
                    files=file_list,
                )
                self._process_sync(slack_event)
            except Exception as e:
                logger.error("Slack on_message error: %s", e)

        handler = SocketModeHandler(app, SLACK_APP_TOKEN)
        self._handler = handler
        logger.info("Slack listener connected via Socket Mode")
        handler.start()  # Blocks until disconnected
