"""Command Queue — lane-aware FIFO queue for message processing.

Inspired by OpenClaw's lane architecture. Instead of the simple debounce + lock
system, messages flow through a proper queue with three modes:

  - COLLECT: Default mode. Accumulates rapid messages and batches them into
    one prompt (replaces the old debounce system).
  - STEER: When the brain is mid-think(), new messages are collected and
    injected as a follow-up turn instead of re-running the full pipeline.
  - FOLLOWUP: Queue messages for the next think() cycle if the current one
    is wrapping up.

Key improvements over the old system:
  - Configurable debounce window (was hardcoded 2.0s)
  - Queue cap prevents unbounded growth
  - Messages arriving mid-processing are batched for follow-up instead of
    being processed independently (which caused duplicate classify+think cycles)
  - Clean separation between queue management and message handling
"""

import asyncio
import logging
import time
from dataclasses import dataclass, field
from enum import Enum, auto
from typing import Any, Callable, Awaitable

logger = logging.getLogger(__name__)


class QueueMode(Enum):
    """Processing mode for queued messages."""
    COLLECT = auto()   # Batching rapid messages
    STEER = auto()     # Injecting into running session
    FOLLOWUP = auto()  # Queued for next cycle


@dataclass
class QueuedMessage:
    """A single message waiting in the queue."""
    text: str
    update: Any  # telegram.Update
    timestamp: float = field(default_factory=time.monotonic)


@dataclass
class MessageBatch:
    """A batch of messages ready for processing."""
    messages: list[QueuedMessage]
    combined_text: str
    last_update: Any  # Use the last update for replies

    @property
    def count(self) -> int:
        return len(self.messages)


class UserQueue:
    """Per-user message queue with debounce and batching.

    Lifecycle:
      1. Messages arrive via enqueue()
      2. After debounce_seconds of silence, the batch fires
      3. If more messages arrive while processing, they go into a follow-up batch
      4. When processing completes, follow-up batch fires immediately
    """

    def __init__(
        self,
        user_id: str,
        debounce_seconds: float = 2.0,
        max_queue_size: int = 20,
    ):
        self.user_id = user_id
        self.debounce_seconds = debounce_seconds
        self.max_queue_size = max_queue_size

        # Current batch being accumulated
        self._pending: list[QueuedMessage] = []
        self._debounce_task: asyncio.Task | None = None

        # Follow-up messages that arrived during processing
        self._followup: list[QueuedMessage] = []

        # Processing state
        self._processing = False
        self._lock = asyncio.Lock()

        # Callback for when a batch is ready
        self._on_batch: Callable[[MessageBatch], Awaitable[None]] | None = None

    @property
    def is_processing(self) -> bool:
        """Whether this user's messages are currently being processed."""
        return self._processing

    @property
    def is_locked(self) -> bool:
        """Whether the processing lock is held."""
        return self._lock.locked()

    @property
    def pending_count(self) -> int:
        """Number of messages waiting to be processed."""
        return len(self._pending) + len(self._followup)

    def set_handler(self, handler: Callable[[MessageBatch], Awaitable[None]]):
        """Set the callback for when a message batch is ready."""
        self._on_batch = handler

    async def enqueue(self, text: str, update: Any):
        """Add a message to the queue.

        If not currently processing: starts/resets the debounce timer.
        If currently processing: adds to follow-up batch (processed after
        current batch completes).
        """
        msg = QueuedMessage(text=text, update=update)

        if self._processing:
            # Brain is mid-think() — queue as follow-up
            if len(self._followup) < self.max_queue_size:
                self._followup.append(msg)
                logger.info(
                    "User %s: message queued as follow-up (%d in queue)",
                    self.user_id, len(self._followup),
                )
            else:
                # Queue full — summarize oldest messages
                logger.warning(
                    "User %s: follow-up queue full (%d), dropping oldest",
                    self.user_id, self.max_queue_size,
                )
                self._followup.pop(0)
                self._followup.append(msg)
            return

        # Not processing — add to pending batch, start/reset debounce
        if len(self._pending) < self.max_queue_size:
            self._pending.append(msg)
        else:
            self._pending.pop(0)
            self._pending.append(msg)

        # Cancel existing debounce timer
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()

        # Start new debounce timer
        self._debounce_task = asyncio.create_task(self._debounce_fire())

    async def _debounce_fire(self):
        """Wait for debounce window, then fire the batch."""
        await asyncio.sleep(self.debounce_seconds)
        await self._fire_batch()

    async def _fire_batch(self):
        """Process the pending batch, then check for follow-ups."""
        if not self._pending:
            return

        if not self._on_batch:
            logger.error("User %s: no batch handler set!", self.user_id)
            self._pending.clear()
            return

        async with self._lock:
            self._processing = True
            try:
                # Take the pending messages
                messages = self._pending[:]
                self._pending.clear()

                batch = _make_batch(messages)
                if batch.count > 1:
                    logger.info(
                        "User %s: firing batch of %d messages",
                        self.user_id, batch.count,
                    )

                await self._on_batch(batch)

                # After processing, check if follow-up messages accumulated
                while self._followup:
                    followup_messages = self._followup[:]
                    self._followup.clear()

                    followup_batch = _make_batch(followup_messages)
                    logger.info(
                        "User %s: processing %d follow-up messages",
                        self.user_id, followup_batch.count,
                    )
                    await self._on_batch(followup_batch)

            finally:
                self._processing = False

    def try_acquire_for_system(self) -> bool:
        """Try to acquire the lock for a system task (heartbeat, curation).

        Returns True if the lock was acquired (caller must release via
        release_for_system()). Returns False if the user is busy.

        Non-blocking: does not wait.
        """
        if self._processing:
            return False
        if self._lock.locked():
            return False
        return True

    async def acquire_for_system(self) -> bool:
        """Acquire the lock for a system task, setting processing flag.

        Returns False without blocking if lock is held or user is busy.
        """
        if self._processing:
            return False
        if self._lock.locked():
            return False

        # Actually acquire the lock
        acquired = self._lock.locked()
        if not acquired:
            await self._lock.acquire()
            self._processing = True
            return True
        return False

    def release_for_system(self):
        """Release the lock after a system task completes."""
        self._processing = False
        if self._lock.locked():
            self._lock.release()


class MessageQueue:
    """Global message queue manager — manages per-user queues.

    Drop-in replacement for the old _pending_messages / _debounce_tasks
    / _user_locks / _active_processing system in bot.py.
    """

    def __init__(
        self,
        debounce_seconds: float = 2.0,
        max_queue_size: int = 20,
    ):
        self.debounce_seconds = debounce_seconds
        self.max_queue_size = max_queue_size
        self._queues: dict[str, UserQueue] = {}
        self._handler: Callable[[str, MessageBatch], Awaitable[None]] | None = None

    def set_handler(self, handler: Callable[[str, MessageBatch], Awaitable[None]]):
        """Set the global handler for message batches.

        handler(user_id, batch) is called when a batch is ready.
        """
        self._handler = handler

    def _get_queue(self, user_id: str) -> UserQueue:
        """Get or create the queue for a user."""
        if user_id not in self._queues:
            q = UserQueue(
                user_id=user_id,
                debounce_seconds=self.debounce_seconds,
                max_queue_size=self.max_queue_size,
            )
            # Bind the handler
            if self._handler:
                handler = self._handler

                async def _bound_handler(batch: MessageBatch, uid=user_id):
                    await handler(uid, batch)

                q.set_handler(_bound_handler)
            self._queues[user_id] = q
        return self._queues[user_id]

    async def enqueue(self, user_id: str, text: str, update: Any):
        """Add a message to the user's queue."""
        q = self._get_queue(user_id)
        await q.enqueue(text, update)

    def is_user_processing(self, user_id: str) -> bool:
        """Check if a user is currently being processed."""
        q = self._queues.get(user_id)
        return q.is_processing if q else False

    def is_user_locked(self, user_id: str) -> bool:
        """Check if a user's lock is held."""
        q = self._queues.get(user_id)
        return q.is_locked if q else False

    async def acquire_for_system(self, user_id: str) -> UserQueue | None:
        """Try to acquire the user's queue for a system task.

        Returns the UserQueue if acquired (caller must call release_for_system()),
        or None if the user is busy.
        """
        q = self._get_queue(user_id)
        if await q.acquire_for_system():
            return q
        return None

    def release_for_system(self, user_id: str):
        """Release the user's queue after a system task."""
        q = self._queues.get(user_id)
        if q:
            q.release_for_system()


def _make_batch(messages: list[QueuedMessage]) -> MessageBatch:
    """Create a MessageBatch from a list of QueuedMessages."""
    texts = [m.text for m in messages]
    combined = "\n".join(texts)
    last_update = messages[-1].update
    return MessageBatch(
        messages=messages,
        combined_text=combined,
        last_update=last_update,
    )
