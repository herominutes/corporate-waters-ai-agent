"""HTTP API server — OpenAI-compatible endpoint for VisionClaw / external integrations.

Exposes POST /v1/chat/completions so smart glasses (or any client) can route
tool calls through Saul's brain pipeline (web search, memory, calendar, email).

Health check: GET /v1/chat/completions → {"status": "ok"}

Message steering (inspired by OpenClaw's collect mode):
  - Rapid successive messages are debounced and combined into one request
  - Each HTTP client waits and gets the same combined response
  - If the brain is already processing, new requests get 429

Stateless: glass messages are NOT saved to conversations table, so they
never pollute Telegram history. The brain reads Telegram context + memories.
"""

import asyncio
import json
import logging
import time

from aiohttp import web

from goodman.config import API_HOST, API_PORT, API_SECRET, TELEGRAM_ALLOWED_USER_ID

logger = logging.getLogger(__name__)

# --- Server state ---
_runner: web.AppRunner | None = None

# --- Collect/steer state ---
DEBOUNCE_SECONDS = 1.0  # Wait for quiet before firing (matches OpenClaw default)

_pending: list[tuple[str, asyncio.Future]] = []  # (message, response_future)
_debounce_task: asyncio.Task | None = None
_busy = False  # True while brain is processing
_state_lock = asyncio.Lock()  # Guards _pending, _busy, _debounce_task


def _check_auth(request: web.Request) -> bool:
    """Validate Bearer token from Authorization header."""
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Bearer "):
        return False
    return auth[7:] == API_SECRET


async def handle_health(request: web.Request) -> web.Response:
    """GET /v1/chat/completions — health check (VisionClaw polls this)."""
    return web.json_response({"status": "ok"})


async def handle_chat(request: web.Request) -> web.Response:
    """POST /v1/chat/completions — OpenAI-compatible chat endpoint.

    Messages are collected and debounced: if multiple arrive within 1s,
    they're combined into a single brain request. All waiting clients
    receive the same response.
    """
    # Auth check
    if not _check_auth(request):
        return web.json_response(
            {"error": {"message": "Invalid or missing API key", "type": "auth_error"}},
            status=401,
        )

    # Parse request body
    try:
        body = await request.json()
    except (json.JSONDecodeError, Exception):
        return web.json_response(
            {"error": {"message": "Invalid JSON body", "type": "invalid_request"}},
            status=400,
        )

    messages = body.get("messages", [])
    if not messages:
        return web.json_response(
            {"error": {"message": "No messages provided", "type": "invalid_request"}},
            status=400,
        )

    # Extract the last user message
    user_message = None
    for msg in reversed(messages):
        if msg.get("role") == "user" and msg.get("content"):
            user_message = msg["content"]
            break

    if not user_message:
        return web.json_response(
            {"error": {"message": "No user message found", "type": "invalid_request"}},
            status=400,
        )

    # Try to enqueue for collect/steer
    future = asyncio.get_event_loop().create_future()

    async with _state_lock:
        if _busy:
            # Brain is mid-think — reject (VisionClaw retries on its own)
            logger.info("API busy, rejecting: %.60s...", user_message)
            return web.json_response(
                {"error": {"message": "Already processing a request. Try again shortly.", "type": "rate_limit"}},
                status=429,
                headers={"Retry-After": "5"},
            )

        # Add to pending batch
        _pending.append((user_message, future))

        if len(_pending) > 1:
            logger.info(
                "API collect: bundling message #%d: %.60s...",
                len(_pending), user_message,
            )

        # Reset debounce timer
        _reset_debounce()

    # Wait for the combined response (all clients in this batch share it)
    try:
        response_text = await asyncio.wait_for(future, timeout=120)
    except asyncio.TimeoutError:
        return web.json_response(
            {"error": {"message": "Request timed out", "type": "timeout"}},
            status=504,
        )
    except Exception as e:
        logger.error("API handler error: %s", e, exc_info=True)
        return web.json_response(
            {"error": {"message": "Internal server error", "type": "server_error"}},
            status=500,
        )

    # Return OpenAI-compatible response
    return web.json_response({
        "id": f"glass-{int(time.time())}",
        "object": "chat.completion",
        "choices": [{
            "index": 0,
            "message": {
                "role": "assistant",
                "content": response_text,
            },
            "finish_reason": "stop",
        }],
    })


def _reset_debounce():
    """Cancel existing debounce timer and start a new one.

    Must be called while holding _state_lock.
    """
    global _debounce_task
    if _debounce_task and not _debounce_task.done():
        _debounce_task.cancel()
    _debounce_task = asyncio.create_task(_debounce_fire())


async def _debounce_fire():
    """Wait for debounce window, then fire the collected batch."""
    await asyncio.sleep(DEBOUNCE_SECONDS)
    await _fire_batch()


async def _fire_batch():
    """Combine pending messages and process them through the brain."""
    global _busy

    async with _state_lock:
        if not _pending:
            return
        # Take the batch
        batch = _pending[:]
        _pending.clear()
        _busy = True

    messages = [msg for msg, _ in batch]
    futures = [fut for _, fut in batch]

    # Combine messages: if multiple, join with newlines
    if len(messages) == 1:
        combined = messages[0]
    else:
        combined = "\n".join(messages)
        logger.info("API firing batch of %d messages: %.100s...", len(messages), combined)

    user_id = TELEGRAM_ALLOWED_USER_ID

    try:
        response_text = await _process_message(user_id, combined)
        # Resolve all waiting clients with the same response
        for fut in futures:
            if not fut.done():
                fut.set_result(response_text)
    except Exception as e:
        for fut in futures:
            if not fut.done():
                fut.set_exception(e)
    finally:
        _busy = False


async def _process_message(user_id: str, message: str) -> str:
    """Run message through Saul's brain — think() handles everything via tools.

    Stateless: does NOT save messages to conversations table, so glass chatter
    never pollutes Telegram history. The brain still gets full context from
    system prompt + memories + recent Telegram messages.
    """
    from goodman import brain

    # Build enriched message with context prefix
    enriched = (
        "[This message is from the boss via smart glasses (VisionClaw). "
        "Keep responses concise and conversational — no markdown formatting, "
        "no bullet points, no headers. Speak naturally as if talking. "
        "The response will be read aloud by TTS. "
        "Do NOT send emails from glasses — if the boss wants to send email, "
        "tell him to use Telegram for that.]\n\n"
        + message
    )

    # Think (reads Telegram history + memories for context, but doesn't write back)
    response = await brain.think(user_id, enriched)

    clean_response = brain._strip_markdown(response) if response else response
    return clean_response or response


async def start():
    """Start the API server. No-op if API_SECRET is not configured."""
    global _runner

    if not API_SECRET:
        logger.info("API server disabled (API_SECRET not set).")
        return

    app = web.Application()
    app.router.add_get("/v1/chat/completions", handle_health)
    app.router.add_post("/v1/chat/completions", handle_chat)

    _runner = web.AppRunner(app)
    await _runner.setup()
    site = web.TCPSite(_runner, API_HOST, API_PORT)
    await site.start()
    logger.info("API server running on %s:%s", API_HOST, API_PORT)


async def stop():
    """Stop the API server."""
    global _runner
    if _runner:
        await _runner.cleanup()
        _runner = None
        logger.info("API server stopped.")
