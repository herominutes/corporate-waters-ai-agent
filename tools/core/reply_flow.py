"""Cross-platform reply confirmation flow.

Manages pending reply drafts for Telegram DM and WhatsApp messages.
Users see a draft preview with Approve / Edit / Cancel buttons before
any message is actually sent through the userbot or WhatsApp sidecar.

Same pattern as the email confirmation flow in bot.py, but generalized
to support multiple platforms via PendingReply.platform.
"""

import time
import uuid
import logging
from dataclasses import dataclass, field
from telegram import InlineKeyboardButton, InlineKeyboardMarkup

logger = logging.getLogger(__name__)

EXPIRY_SECONDS = 300  # 5 minutes


@dataclass
class PendingReply:
    """A draft reply waiting for user confirmation."""
    id: str = field(default_factory=lambda: uuid.uuid4().hex[:8])
    platform: str = ""          # "telegram_dm" or "whatsapp"
    target_chat_id: str = ""    # chat/phone to send to
    target_name: str = ""       # display name of recipient
    original_message: str = ""  # the message being replied to
    draft_text: str = ""        # the drafted reply
    created_at: float = field(default_factory=time.time)


# In-memory store — keyed by reply ID
_pending_replies: dict[str, PendingReply] = {}


def store_pending(reply: PendingReply) -> str:
    """Store a pending reply and return its ID."""
    _cleanup_expired()
    _pending_replies[reply.id] = reply
    logger.info("Stored pending reply %s for %s → %s", reply.id, reply.platform, reply.target_name)
    return reply.id


def get_pending(reply_id: str) -> PendingReply | None:
    """Get a pending reply by ID, or None if expired/missing."""
    _cleanup_expired()
    return _pending_replies.get(reply_id)


def discard_pending(reply_id: str) -> bool:
    """Discard a pending reply. Returns True if it existed."""
    return _pending_replies.pop(reply_id, None) is not None


def build_keyboard(reply_id: str) -> InlineKeyboardMarkup:
    """Build the Approve / Edit / Cancel inline keyboard for a reply."""
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("Approve", callback_data=f"reply_approve:{reply_id}"),
            InlineKeyboardButton("Edit", callback_data=f"reply_edit:{reply_id}"),
            InlineKeyboardButton("Cancel", callback_data=f"reply_cancel:{reply_id}"),
        ]
    ])


def build_preview(reply: PendingReply) -> str:
    """Build a human-readable preview of the pending reply."""
    platform_label = "Telegram DM" if reply.platform == "telegram_dm" else "WhatsApp"
    return (
        f"Replying to {reply.target_name} ({platform_label}):\n\n"
        f"Their message:\n{reply.original_message[:300]}\n\n"
        f"---\nYour reply:\n{reply.draft_text[:500]}"
    )


def _cleanup_expired():
    """Remove expired pending replies."""
    now = time.time()
    expired = [rid for rid, r in _pending_replies.items()
               if now - r.created_at > EXPIRY_SECONDS]
    for rid in expired:
        _pending_replies.pop(rid, None)
    if expired:
        logger.debug("Cleaned up %d expired pending replies", len(expired))
