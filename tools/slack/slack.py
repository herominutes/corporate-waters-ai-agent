"""Slack integration for The Goodman Protocol.

Stateless function module — all functions synchronous, called via
asyncio.to_thread() from bot.py. Uses slack_sdk.WebClient directly.

Channel-level access control: SLACK_RESTRICTED_CHANNELS are only readable
by privileged users. The _caller_privileged thread-local flag gates access.
"""

import logging
import threading
from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from goodman.config import SLACK_BOT_TOKEN, SLACK_USER_TOKEN, SLACK_RESTRICTED_CHANNELS

logger = logging.getLogger(__name__)

_bot_client: WebClient | None = None
_user_client: WebClient | None = None
_user_cache: dict[str, str] = {}   # user_id -> display_name
_channel_cache: dict[str, str] = {}  # channel_id -> #name
_bot_user_id: str | None = None

_caller_ctx = threading.local()


def set_caller_privileged(privileged: bool):
    """Set whether the current caller is privileged (thread-local)."""
    _caller_ctx.privileged = privileged


def _caller_role() -> str:
    """Return caller role: 'owner', 'privileged', or 'restricted'.

    Checked via env var SLACK_CALLER_ROLE (set per-subprocess) or
    thread-local flag. Default is 'owner' (Telegram / internal calls).
    """
    import os
    env_role = os.environ.get("SLACK_CALLER_ROLE")
    if env_role in ("owner", "privileged", "restricted"):
        return env_role
    # Legacy env flag
    if os.environ.get("SLACK_CALLER_RESTRICTED") == "1":
        return "restricted"
    tl = getattr(_caller_ctx, "privileged", None)
    if tl is not None:
        return "privileged" if tl else "restricted"
    return "owner"


def _is_restricted_channel(channel_name: str) -> bool:
    """Check if a channel is restricted for the current caller."""
    role = _caller_role()
    if role == "owner":
        return False
    name = channel_name.lstrip("#").lower()
    return name in SLACK_RESTRICTED_CHANNELS


def _is_dm_blocked() -> bool:
    """DMs are only accessible to the owner."""
    return _caller_role() != "owner"


def _get_bot_client() -> WebClient | None:
    """Lazy singleton for the bot client."""
    global _bot_client
    if not SLACK_BOT_TOKEN:
        return None
    if _bot_client is None:
        _bot_client = WebClient(token=SLACK_BOT_TOKEN)
    return _bot_client


def _get_user_client() -> WebClient | None:
    """Lazy singleton for the user client (needed for search)."""
    global _user_client
    if not SLACK_USER_TOKEN:
        return None
    if _user_client is None:
        _user_client = WebClient(token=SLACK_USER_TOKEN)
    return _user_client


def _get_bot_user_id() -> str | None:
    """Get the bot's own user ID (cached)."""
    global _bot_user_id
    if _bot_user_id:
        return _bot_user_id
    client = _get_bot_client()
    if not client:
        return None
    try:
        resp = client.auth_test()
        _bot_user_id = resp["user_id"]
        return _bot_user_id
    except SlackApiError as e:
        logger.error("Slack auth_test failed: %s", e)
        return None


def resolve_user(user_id: str) -> str:
    """Resolve a user_id to a display name (cached)."""
    if user_id in _user_cache:
        return _user_cache[user_id]
    client = _get_bot_client()
    if not client:
        return user_id
    try:
        resp = client.users_info(user=user_id)
        user = resp["user"]
        name = user.get("real_name") or user.get("name") or user_id
        _user_cache[user_id] = name
        return name
    except SlackApiError:
        return user_id


def resolve_channel(channel_id: str) -> str:
    """Resolve a channel_id to a #name (cached)."""
    if channel_id in _channel_cache:
        return _channel_cache[channel_id]
    client = _get_bot_client()
    if not client:
        return channel_id
    try:
        resp = client.conversations_info(channel=channel_id)
        ch = resp["channel"]
        name = "#" + ch.get("name", channel_id)
        _channel_cache[channel_id] = name
        return name
    except SlackApiError:
        return channel_id


def _find_channel_id(channel_name: str) -> str | None:
    """Find a channel ID by name (strips leading #)."""
    name = channel_name.lstrip("#").lower()
    client = _get_bot_client()
    if not client:
        return None

    # Check cache first
    for cid, cname in _channel_cache.items():
        if cname.lstrip("#").lower() == name:
            return cid

    # Search via API
    try:
        cursor = None
        while True:
            kwargs = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_list(**kwargs)
            for ch in resp["channels"]:
                _channel_cache[ch["id"]] = "#" + ch["name"]
                if ch["name"].lower() == name:
                    return ch["id"]
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break
    except SlackApiError as e:
        logger.error("Slack conversations_list error: %s", e)
    return None


def _find_user_id(user_name: str) -> str | None:
    """Find a Slack user ID by name.

    Uses cached user list to avoid repeated API calls.
    Matching priority: exact full name > exact handle > substring match.
    """
    client = _get_bot_client()
    if not client:
        return None

    query = user_name.lower().strip()
    if not query:
        return None

    try:
        members = _get_members_cached(client)

        # Pass 1: exact match on real_name, display_name, or handle
        for u in members:
            uname = (u.get("real_name") or "").lower()
            display = (u.get("profile", {}).get("display_name") or "").lower()
            handle = (u.get("name") or "").lower()
            if query == uname or (display and query == display) or query == handle:
                return u["id"]

        # Pass 2: query is a substring of real_name or display_name (non-empty only)
        for u in members:
            uname = (u.get("real_name") or "").lower()
            display = (u.get("profile", {}).get("display_name") or "").lower()
            if (uname and query in uname) or (display and query in display):
                return u["id"]

        # Pass 3: first-name match (only when query is a single word)
        if " " not in query:
            for u in members:
                uname = (u.get("real_name") or "").lower()
                first_name = uname.split()[0] if uname else ""
                if first_name and query == first_name:
                    return u["id"]

        return None
    except SlackApiError as e:
        logger.error("Slack user lookup error: %s", e)
    return None


_members_cache: list[dict] | None = None
_members_cache_ts: float = 0


def _get_members_cached(client: WebClient, ttl: float = 300) -> list[dict]:
    """Return workspace members, cached for *ttl* seconds."""
    import time
    global _members_cache, _members_cache_ts
    now = time.monotonic()
    if _members_cache is not None and (now - _members_cache_ts) < ttl:
        return _members_cache

    resp = client.users_list()
    members = [
        u for u in resp["members"]
        if not u.get("deleted") and not u.get("is_bot") and u.get("id") != "USLACKBOT"
    ]
    _members_cache = members
    _members_cache_ts = now
    return members


def _find_dm_channel(user_name: str) -> str | None:
    """Find or open a DM channel with a user by name."""
    user_id = _find_user_id(user_name)
    if not user_id:
        return None
    client = _get_bot_client()
    if not client:
        return None
    try:
        dm_resp = client.conversations_open(users=[user_id])
        return dm_resp["channel"]["id"]
    except SlackApiError as e:
        logger.error("Slack user lookup error: %s", e)
    return None


def _resolve_channel_or_dm(channel: str) -> str | None:
    """Resolve a channel name, ID, or user name to a channel ID."""
    import re
    # Already a Slack ID (e.g. C0AFM83R4DP, D0AFM83R4DP) — uppercase alphanumeric
    if re.fullmatch(r'[CDGW][A-Z0-9]{8,}', channel):
        return channel

    # Try as channel name
    cid = _find_channel_id(channel)
    if cid:
        return cid

    # Try as user name (DM)
    return _find_dm_channel(channel)


def _format_message(msg: dict) -> str:
    """Format a single Slack message for display."""
    user = resolve_user(msg.get("user", "unknown"))
    text = msg.get("text", "")
    ts = msg.get("ts", "")
    # Convert timestamp to readable time
    try:
        from datetime import datetime
        dt = datetime.fromtimestamp(float(ts))
        time_str = dt.strftime("%H:%M")
    except (ValueError, TypeError):
        time_str = ts
    return f"[{time_str}] {user}: {text}"


# ------------------------------------------------------------------
# Public API functions — all return formatted strings
# ------------------------------------------------------------------

def get_unread_dms(limit: int = 20) -> str:
    """Get recent DMs sent to the bot."""
    if _is_dm_blocked():
        return "Access denied: DM history is not available in this context."
    client = _get_bot_client()
    if not client:
        return "Slack not configured."
    try:
        bot_id = _get_bot_user_id()
        # List IM conversations
        resp = client.conversations_list(types="im", limit=50)
        channels = resp.get("channels", [])
        if not channels:
            return "No DM conversations found."

        all_messages = []
        for ch in channels[:10]:  # Check last 10 DM channels
            try:
                hist = client.conversations_history(channel=ch["id"], limit=limit)
                for msg in hist.get("messages", []):
                    # Skip bot's own messages
                    if msg.get("user") == bot_id or msg.get("bot_id"):
                        continue
                    user = resolve_user(msg.get("user", "unknown"))
                    all_messages.append((float(msg.get("ts", 0)), user, msg.get("text", "")))
            except SlackApiError:
                continue

        if not all_messages:
            return "No recent DMs."

        # Sort by timestamp, most recent first
        all_messages.sort(key=lambda x: x[0], reverse=True)
        all_messages = all_messages[:limit]

        lines = []
        for ts, user, text in all_messages:
            try:
                from datetime import datetime
                dt = datetime.fromtimestamp(ts)
                time_str = dt.strftime("%b %d %H:%M")
            except (ValueError, TypeError):
                time_str = "?"
            lines.append(f"[{time_str}] {user}: {text}")
        return f"Recent Slack DMs ({len(lines)}):\n" + "\n".join(lines)

    except SlackApiError as e:
        logger.error("Slack get_unread_dms error: %s", e)
        return f"Slack error: {e.response['error']}"


def get_channel_messages(channel: str, limit: int = 20) -> str:
    """Get recent messages from a channel (by name or ID)."""
    if _is_restricted_channel(channel):
        return f"Access denied: #{channel.lstrip('#')} is a restricted channel."
    # Block DM channel access for non-owner callers
    import re as _re
    if _is_dm_blocked() and _re.fullmatch(r'D[A-Z0-9]{8,}', channel):
        return "Access denied: DM conversations are not available in this context."
    client = _get_bot_client()
    if not client:
        return "Slack not configured."
    try:
        channel_id = _resolve_channel_or_dm(channel)
        if not channel_id:
            return f"Channel '{channel}' not found."

        # Block DMs by resolved ID too
        if _is_dm_blocked() and channel_id.startswith("D"):
            return "Access denied: DM conversations are not available in this context."

        # Check restriction by resolved name too (in case caller passed an ID)
        resolved_name = resolve_channel(channel_id)
        if _is_restricted_channel(resolved_name):
            return f"Access denied: {resolved_name} is a restricted channel."

        # Try to join if it's a public channel
        try:
            client.conversations_join(channel=channel_id)
        except SlackApiError:
            pass  # Already in channel or private

        resp = client.conversations_history(channel=channel_id, limit=limit)
        messages = resp.get("messages", [])
        if not messages:
            return f"No messages in {channel}."

        channel_name = resolve_channel(channel_id)
        lines = [_format_message(m) for m in reversed(messages)]
        return f"Messages in {channel_name} (last {len(lines)}):\n" + "\n".join(lines)

    except SlackApiError as e:
        logger.error("Slack get_channel_messages error: %s", e)
        return f"Slack error: {e.response['error']}"


def send_message(channel: str, text: str, thread_ts: str | None = None, as_bot: bool = False) -> str:
    """Post a message to a channel or DM.

    Args:
        as_bot: If True, always send as the bot (skip user token).
                Used for Saul's own replies to Slack users.
    """
    import os
    # SECURITY GATE: a non-owner Slack caller (SLACK_CALLER_ROLE set by brain.py) may NEVER
    # post via Alex's personal user token — that is identity impersonation. Force the bot
    # identity for them. Only the owner (Telegram / internal, env unset) can send "as you".
    if os.getenv("SLACK_CALLER_ROLE"):
        as_bot = True
        from goodman import config as _cfg
        if not _cfg.caller_has(_cfg.CAP_SLACK):
            return "You're not authorized to send Slack messages on Alex's behalf."
    bot_client = _get_bot_client()
    user_client = _get_user_client()
    if not bot_client:
        return "Slack not configured."
    try:
        # Check if this is a DM to a user (not a channel ID or #channel name)
        import re
        is_channel_id = bool(re.fullmatch(r'[CDGW][A-Z0-9]{8,}', channel))
        is_channel_name = channel.startswith('#') or _find_channel_id(channel) is not None

        # For DMs: resolve user ID for user token, DM channel for bot token
        user_id_for_dm = None
        if not is_channel_id and not is_channel_name:
            user_id_for_dm = _find_user_id(channel)

        channel_id = _resolve_channel_or_dm(channel)
        if not channel_id and not user_id_for_dm:
            return f"Channel/user '{channel}' not found."

        # Try user token first (sends as the user), fall back to bot
        if user_client and not as_bot:
            try:
                # For DMs, use user ID directly (user token can't see bot-opened DM channels)
                target = user_id_for_dm if user_id_for_dm else channel_id
                kwargs = {"channel": target, "text": text}
                if thread_ts:
                    kwargs["thread_ts"] = thread_ts
                user_client.chat_postMessage(**kwargs)
                return f"Message sent to {channel} (as you)"
            except SlackApiError as e:
                logger.warning("User token send failed (%s), falling back to bot", e.response['error'])

        if not channel_id:
            return f"Channel/user '{channel}' not found."

        kwargs = {"channel": channel_id, "text": text}
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        bot_client.chat_postMessage(**kwargs)
        channel_name = resolve_channel(channel_id)
        return f"Message sent to {channel_name} (as bot)"

    except SlackApiError as e:
        logger.error("Slack send_message error: %s", e)
        return f"Slack error: {e.response['error']}"


def send_file(channel: str, file_path: str, title: str = "",
              initial_comment: str = "", thread_ts: str | None = None) -> str:
    """Upload a file (image/doc) to a Slack channel or DM.

    Mirrors the Telegram [SEND_FILE:] delivery path so Saul's rendered images
    actually reach the user when he's on Slack.
    """
    bot_client = _get_bot_client()
    if not bot_client:
        return "Slack not configured."
    try:
        import re
        channel_id = _resolve_channel_or_dm(channel) or channel
        # files_upload_v2 needs a C/D/G channel id, not a U user id — open a DM.
        if re.fullmatch(r'U[A-Z0-9]{8,}', channel_id):
            opened = bot_client.conversations_open(users=channel_id)
            channel_id = opened["channel"]["id"]
        kwargs = {"channel": channel_id, "file": file_path}
        if title:
            kwargs["title"] = title
        if initial_comment:
            kwargs["initial_comment"] = initial_comment
        if thread_ts:
            kwargs["thread_ts"] = thread_ts
        bot_client.files_upload_v2(**kwargs)
        return f"File sent to {channel}"
    except SlackApiError as e:
        logger.error("Slack send_file error: %s", e)
        return f"Slack file error: {e.response['error']}"
    except Exception as e:
        logger.error("Slack send_file error: %s", e)
        return f"Failed to send file: {e}"


def reply_in_thread(channel: str, thread_ts: str, text: str) -> str:
    """Reply in a thread."""
    return send_message(channel, text, thread_ts=thread_ts)


def search_messages(query: str, limit: int = 20) -> str:
    """Search Slack messages (requires user token)."""
    client = _get_user_client()
    if not client:
        # Fall back to bot client if no user token
        client = _get_bot_client()
        if not client:
            return "Slack not configured."
        return "Slack search requires a user token (SLACK_USER_TOKEN). Bot tokens can't search."

    try:
        resp = client.search_messages(query=query, count=limit, sort="timestamp")
        matches = resp.get("messages", {}).get("matches", [])
        if not matches:
            return f"No Slack messages found for '{query}'."

        lines = []
        filtered = 0
        for m in matches:
            user = m.get("username", "unknown")
            ch_info = m.get("channel", {})
            channel_name = ch_info.get("name", "?")
            if _is_restricted_channel(channel_name):
                filtered += 1
                continue
            # Filter DMs from search results for non-owner callers
            if _is_dm_blocked() and ch_info.get("is_im"):
                filtered += 1
                continue
            text = m.get("text", "")[:200]
            ts = m.get("ts", "")
            try:
                from datetime import datetime
                dt = datetime.fromtimestamp(float(ts))
                time_str = dt.strftime("%b %d %H:%M")
            except (ValueError, TypeError):
                time_str = "?"
            lines.append(f"[{time_str}] #{channel_name} — {user}: {text}")

        if not lines:
            return f"No Slack messages found for '{query}'."

        total = resp.get("messages", {}).get("total", len(matches))
        return f"Slack search results for '{query}' ({total} total, showing {len(lines)}):\n" + "\n".join(lines)

    except SlackApiError as e:
        logger.error("Slack search error: %s", e)
        return f"Slack search error: {e.response['error']}"


def list_channels() -> str:
    """List public and private channels."""
    client = _get_bot_client()
    if not client:
        return "Slack not configured."
    try:
        all_channels = []
        cursor = None
        while True:
            kwargs = {"types": "public_channel,private_channel", "limit": 200}
            if cursor:
                kwargs["cursor"] = cursor
            resp = client.conversations_list(**kwargs)
            for ch in resp["channels"]:
                _channel_cache[ch["id"]] = "#" + ch["name"]
                members = ch.get("num_members", 0)
                purpose = (ch.get("purpose", {}).get("value", "") or "")[:80]
                archived = " [archived]" if ch.get("is_archived") else ""
                all_channels.append(
                    f"  #{ch['name']} ({members} members){archived}"
                    + (f" — {purpose}" if purpose else "")
                )
            cursor = resp.get("response_metadata", {}).get("next_cursor")
            if not cursor:
                break

        if not all_channels:
            return "No channels found."
        return f"Slack channels ({len(all_channels)}):\n" + "\n".join(all_channels)

    except SlackApiError as e:
        logger.error("Slack list_channels error: %s", e)
        return f"Slack error: {e.response['error']}"


def list_users() -> str:
    """List workspace members (filter bots and deleted)."""
    client = _get_bot_client()
    if not client:
        return "Slack not configured."
    try:
        resp = client.users_list()
        members = []
        for u in resp["members"]:
            if u.get("deleted") or u.get("is_bot") or u.get("id") == "USLACKBOT":
                continue
            name = u.get("real_name") or u.get("name") or "?"
            title = u.get("profile", {}).get("title", "")
            display = u.get("profile", {}).get("display_name", "")
            _user_cache[u["id"]] = name
            line = f"  {name}"
            if display and display != name:
                line += f" (@{display})"
            if title:
                line += f" — {title}"
            members.append(line)

        if not members:
            return "No workspace members found."
        return f"Slack workspace members ({len(members)}):\n" + "\n".join(members)

    except SlackApiError as e:
        logger.error("Slack list_users error: %s", e)
        return f"Slack error: {e.response['error']}"


def add_reaction(channel: str, timestamp: str, emoji: str) -> str:
    """Add an emoji reaction to a message."""
    client = _get_bot_client()
    if not client:
        return "Slack not configured."
    try:
        channel_id = _resolve_channel_or_dm(channel)
        if not channel_id:
            return f"Channel '{channel}' not found."
        # Strip colons from emoji name
        emoji = emoji.strip(":")
        client.reactions_add(channel=channel_id, timestamp=timestamp, name=emoji)
        return f"Reacted with :{emoji}: in {resolve_channel(channel_id)}"

    except SlackApiError as e:
        logger.error("Slack add_reaction error: %s", e)
        return f"Slack error: {e.response['error']}"


def upload_file(channel: str, filepath_or_content: str, filename: str, title: str | None = None) -> str:
    """Upload a file to a channel."""
    client = _get_bot_client()
    if not client:
        return "Slack not configured."
    try:
        channel_id = _resolve_channel_or_dm(channel)
        if not channel_id:
            return f"Channel '{channel}' not found."

        from pathlib import Path
        path = Path(filepath_or_content)
        if path.exists():
            client.files_upload_v2(
                channel=channel_id,
                file=str(path),
                filename=filename,
                title=title or filename,
            )
        else:
            client.files_upload_v2(
                channel=channel_id,
                content=filepath_or_content,
                filename=filename,
                title=title or filename,
            )
        return f"File '{filename}' uploaded to {resolve_channel(channel_id)}"

    except SlackApiError as e:
        logger.error("Slack upload_file error: %s", e)
        return f"Slack error: {e.response['error']}"


def get_channel_info(channel: str) -> str:
    """Get channel details — topic, purpose, members count."""
    if _is_restricted_channel(channel):
        return f"Access denied: #{channel.lstrip('#')} is a restricted channel."
    client = _get_bot_client()
    if not client:
        return "Slack not configured."
    try:
        channel_id = _resolve_channel_or_dm(channel)
        if not channel_id:
            return f"Channel '{channel}' not found."

        resolved_name = resolve_channel(channel_id)
        if _is_restricted_channel(resolved_name):
            return f"Access denied: {resolved_name} is a restricted channel."

        resp = client.conversations_info(channel=channel_id)
        ch = resp["channel"]
        name = ch.get("name", "?")
        topic = ch.get("topic", {}).get("value", "") or "(no topic)"
        purpose = ch.get("purpose", {}).get("value", "") or "(no purpose)"
        members = ch.get("num_members", "?")
        created = ch.get("created", 0)
        try:
            from datetime import datetime
            created_str = datetime.fromtimestamp(created).strftime("%Y-%m-%d")
        except (ValueError, TypeError):
            created_str = "?"

        return (
            f"#{name}\n"
            f"  Topic: {topic}\n"
            f"  Purpose: {purpose}\n"
            f"  Members: {members}\n"
            f"  Created: {created_str}"
        )

    except SlackApiError as e:
        logger.error("Slack get_channel_info error: %s", e)
        return f"Slack error: {e.response['error']}"
