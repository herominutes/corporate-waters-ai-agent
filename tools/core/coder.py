"""The Coder - Claude Code CLI agent for executing coding tasks."""

import asyncio
import json
import logging
import os
import sys
from goodman.config import CLAUDE_CMD, PROJECT_ROOT

logger = logging.getLogger(__name__)

WORKSPACE_DIR = "/home/claude"

GENERAL_CODING_PROMPT = """\
You are a coding assistant. Execute the user's coding task directly.
Work in the current directory. Create files, write scripts, and run commands as needed.
Be concise in your output — summarize what you did and what files you created or modified.
"""

SELF_IMPROVE_PROMPT = """\
You are modifying the Goodman Protocol Telegram bot — the very bot you are part of.
This is a REAL coding task. Do the actual work — read files, write code, run tests.
Do NOT summarize what you "would" do. DO IT.

PROJECT STRUCTURE (~/goodman/ — Saul, English bot):
- src/goodman/bot.py — Telegram command handlers, message routing, typing, queue
- src/goodman/brain.py — Claude CLI subprocess, streaming, persona prompt, classifier, memory
- src/goodman/coder.py — Coding agent (this module — you are here)
- src/goodman/config.py — Environment config loader (.env)
- src/goodman/db.py — SQLite database layer (messages, memories, summaries, BM25)
- src/goodman/web.py — Web search (SearXNG), HTTP fetch, Readability+markdown
- src/goodman/browse.py — Persistent stealth browser daemon (Patchright/CDP)
- src/goodman/calendar.py — Google Calendar (OAuth primary, service account fallback)
- src/goodman/gmail.py — Gmail integration (OAuth)
- src/goodman/slack.py / slack_listener.py — Slack integration
- src/goodman/trello.py / trello_cli.py — Trello integration
- src/goodman/context.py — Token-aware context builder for prompts
- src/goodman/session.py — Session lifecycle management
- src/goodman/heartbeat.py — Proactive check-in system
- src/goodman/scheduler.py — Cron-like job scheduler
- src/goodman/queue.py — Per-user message queue with debouncing
- src/goodman/soul.py — Soul file loader (data/soul/*.md)
- src/goodman/agents.py — Sub-agent spawning
- data/soul/SOUL.md — Saul Goodman personality and instructions
- pyproject.toml — Project metadata and dependencies

WIFE BOT (~/goodman-wife/ — Irina, Russian bot):
- Same codebase structure, different configs/souls/data
- If changing shared logic, apply the same change to ~/goodman-wife/

QUALITY STANDARDS:
- Read the existing code FIRST. Understand before modifying.
- Make targeted changes. Don't refactor code you weren't asked to touch.
- Verify imports are correct. Check that functions you call actually exist.
- Keep async patterns consistent. Use logging, not print.

RULES:
- Do NOT modify .env, credentials/, or data/ files.
- Do NOT change the Telegram bot token or security settings.
- Do NOT remove existing functionality unless explicitly asked.
- After making changes, describe EXACTLY what you changed, in which files, and why.
"""


async def execute_coding_task(task: str, is_self_improve: bool = False) -> dict:
    """Run a coding task via Claude Code CLI with tool use enabled.

    Args:
        task: The coding task description.
        is_self_improve: If True, runs in the project directory to modify the bot itself.

    Returns:
        Dict with keys: success, result, cost_usd, num_turns.
    """
    cwd = str(PROJECT_ROOT) if is_self_improve else WORKSPACE_DIR
    system_prompt = SELF_IMPROVE_PROMPT if is_self_improve else GENERAL_CODING_PROMPT

    # Ensure workspace exists for general coding tasks
    if not is_self_improve:
        os.makedirs(WORKSPACE_DIR, exist_ok=True)

    try:
        cmd = [
            CLAUDE_CMD,
            "--print",
            "--output-format", "json",
            "--model", "claude-opus-4-6",
            "--effort", "high",
            "--max-turns", "75",
            "--dangerously-skip-permissions",
            "--permission-mode", "bypassPermissions",
            "--no-session-persistence",
            "--append-system-prompt", system_prompt,
        ]

        # Grant access to key directories
        dirs = {"/home/claude", str(PROJECT_ROOT), WORKSPACE_DIR}
        if is_self_improve:
            dirs.add("/home/claude/goodman-wife")
        for d in dirs:
            cmd.extend(["--add-dir", d])

        cmd.append(task)

        # Clear CLAUDECODE env var so CLI doesn't think it's nested
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
            env=env,
            limit=10 * 1024 * 1024,  # 10MB readline buffer
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=900)

        if proc.returncode != 0:
            error_msg = stderr.decode().strip()
            logger.error("Coder CLI error (rc=%d): %s", proc.returncode, error_msg)
            return {
                "success": False,
                "result": f"CLI error: {error_msg[:500]}",
                "cost_usd": 0,
                "num_turns": 0,
            }

        return _parse_cli_output(stdout.decode())

    except asyncio.TimeoutError:
        logger.error("Coder CLI timed out after 15 minutes")
        return {
            "success": False,
            "result": "The coding task timed out after 15 minutes.",
            "cost_usd": 0,
            "num_turns": 0,
        }
    except FileNotFoundError:
        logger.error("Claude CLI not found at: %s", CLAUDE_CMD)
        return {
            "success": False,
            "result": "Claude CLI not found. Check installation.",
            "cost_usd": 0,
            "num_turns": 0,
        }


def _parse_cli_output(raw: str) -> dict:
    """Parse the JSON output from Claude CLI."""
    try:
        data = json.loads(raw)
        # Extract the assistant's text response from the result
        result_text = ""
        cost_usd = data.get("cost_usd", 0)
        num_turns = data.get("num_turns", 0)

        # The result field contains the final message content
        result = data.get("result", "")
        if isinstance(result, str):
            result_text = result
        elif isinstance(result, list):
            # Content blocks format
            for block in result:
                if isinstance(block, dict) and block.get("type") == "text":
                    result_text += block.get("text", "") + "\n"
            result_text = result_text.strip()

        if not result_text:
            result_text = "Task completed but no output was returned."

        return {
            "success": True,
            "result": result_text,
            "cost_usd": cost_usd,
            "num_turns": num_turns,
        }
    except (json.JSONDecodeError, KeyError) as e:
        logger.error("Failed to parse coder output: %s", e)
        # Fall back to raw text
        return {
            "success": True,
            "result": raw[:2000] if raw else "Task completed (no output).",
            "cost_usd": 0,
            "num_turns": 0,
        }


def restart_bot():
    """Restart the bot process after self-modification using os.execv."""
    logger.info("Restarting bot after self-modification...")
    python = sys.executable
    os.execv(python, [python, "-m", "goodman.bot"])
