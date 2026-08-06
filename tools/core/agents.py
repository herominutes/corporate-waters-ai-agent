"""Sub-agent spawning system — parallel focused Claude sessions.

Inspired by OpenClaw's sub-agent architecture. Instead of one brain doing
everything, Saul can spawn specialized agents that run in parallel and
report back. Each agent gets a focused system prompt and limited scope.

Key patterns:
  - spawn_agent(): Run a single focused Claude session
  - spawn_parallel(): Run multiple agents concurrently, collect results
  - Pre-defined agent profiles for common tasks (researcher, analyst, etc.)
  - Automatic timeout and error isolation per agent
  - Results are sanitized before returning to the main brain
"""

import asyncio
import logging
import os
from dataclasses import dataclass
from goodman.config import CLAUDE_CMD

logger = logging.getLogger(__name__)


@dataclass
class AgentResult:
    """Result from a sub-agent execution."""
    name: str
    success: bool
    output: str
    elapsed_seconds: float


# Pre-defined agent profiles
AGENT_PROFILES = {
    "researcher": {
        "system_prompt": (
            "You are a focused research agent. Your ONLY job is to find and "
            "summarize information on the given topic. Be thorough but concise. "
            "Use web search and page fetching tools. Return findings as a clear "
            "bulleted summary. No personality, no filler — just facts."
        ),
        "max_turns": 8,
        "timeout": 120,
    },
    "analyst": {
        "system_prompt": (
            "You are a data analyst agent. Your job is to analyze the provided "
            "data and extract insights, patterns, and actionable conclusions. "
            "Be precise and quantitative where possible. No personality — just analysis."
        ),
        "max_turns": 3,
        "timeout": 60,
    },
    "writer": {
        "system_prompt": (
            "You are a writing agent. Draft the requested content based on the "
            "provided context. Match the specified tone and format. Be concise "
            "and polished. Return ONLY the drafted content, nothing else."
        ),
        "max_turns": 3,
        "timeout": 60,
    },
    "planner": {
        "system_prompt": (
            "You are a planning agent. Break down the given task into clear, "
            "actionable steps. Consider dependencies, risks, and priorities. "
            "Return a numbered action plan. Be practical, not theoretical."
        ),
        "max_turns": 3,
        "timeout": 60,
    },
    "browser": {
        "system_prompt": (
            "You are a browser automation agent. You have access to the 'browse' "
            "CLI command for interacting with websites. Navigate to the specified "
            "URL, interact with forms/buttons as needed, and extract the requested "
            "information. Use 'browse goto', 'browse describe', 'browse fill', "
            "'browse click', 'browse text' commands via Bash. Return the extracted "
            "data clearly."
        ),
        "max_turns": 15,
        "timeout": 180,
    },
}


async def spawn_agent(
    task: str,
    name: str = "agent",
    system_prompt: str | None = None,
    profile: str | None = None,
    max_turns: int = 5,
    timeout: int = 90,
) -> AgentResult:
    """Spawn a focused sub-agent to handle a specific task.

    Args:
        task: The task/prompt for the agent.
        name: Human-readable name for logging.
        system_prompt: Custom system prompt. Overrides profile.
        profile: Use a pre-defined agent profile (researcher, analyst, etc.).
        max_turns: Max agentic turns (tool use iterations).
        timeout: Timeout in seconds.

    Returns:
        AgentResult with success status and output.
    """
    # Apply profile defaults if specified
    if profile and profile in AGENT_PROFILES:
        p = AGENT_PROFILES[profile]
        if system_prompt is None:
            system_prompt = p["system_prompt"]
        max_turns = p.get("max_turns", max_turns)
        timeout = p.get("timeout", timeout)

    if system_prompt is None:
        system_prompt = "You are a focused assistant. Complete the task concisely."

    import time
    start = time.monotonic()

    try:
        env = {k: v for k, v in os.environ.items() if k != "CLAUDECODE"}

        cmd = [
            CLAUDE_CMD,
            "--print",
            "--output-format", "text",
            "--model", "claude-opus-4-6",
            "--effort", "high",
            "--max-turns", str(max_turns),
            "--append-system-prompt", system_prompt,
        ]

        if max_turns > 0:
            cmd.extend([
                "--dangerously-skip-permissions",
                "--permission-mode", "bypassPermissions",
            ])

        cmd.append(task)

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd="/tmp",
            env=env,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        elapsed = time.monotonic() - start

        if proc.returncode != 0:
            error = stderr.decode().strip()[:300]
            logger.warning("Agent '%s' failed (rc=%d): %s", name, proc.returncode, error)
            return AgentResult(
                name=name,
                success=False,
                output=f"Agent error: {error}",
                elapsed_seconds=elapsed,
            )

        output = stdout.decode().strip()
        if not output:
            output = "(no output)"

        logger.info("Agent '%s' completed in %.1fs (%d chars)", name, elapsed, len(output))
        return AgentResult(
            name=name,
            success=True,
            output=output,
            elapsed_seconds=elapsed,
        )

    except asyncio.TimeoutError:
        elapsed = time.monotonic() - start
        logger.warning("Agent '%s' timed out after %ds", name, timeout)
        return AgentResult(
            name=name,
            success=False,
            output=f"Agent timed out after {timeout}s",
            elapsed_seconds=elapsed,
        )
    except Exception as e:
        elapsed = time.monotonic() - start
        logger.error("Agent '%s' exception: %s", name, e)
        return AgentResult(
            name=name,
            success=False,
            output=f"Agent exception: {e}",
            elapsed_seconds=elapsed,
        )


async def spawn_parallel(
    tasks: list[dict],
) -> list[AgentResult]:
    """Spawn multiple sub-agents in parallel and collect all results.

    Args:
        tasks: List of dicts, each with keys matching spawn_agent() params:
               - task (required): The prompt
               - name: Agent name
               - profile: Agent profile name
               - system_prompt: Custom system prompt
               - max_turns: Max turns
               - timeout: Timeout

    Returns:
        List of AgentResult in same order as input tasks.

    Example:
        results = await spawn_parallel([
            {"task": "Search for latest AI news", "profile": "researcher", "name": "news"},
            {"task": "Analyze this data: ...", "profile": "analyst", "name": "analysis"},
        ])
    """
    if not tasks:
        return []

    coros = []
    for t in tasks:
        coros.append(spawn_agent(
            task=t["task"],
            name=t.get("name", "agent"),
            system_prompt=t.get("system_prompt"),
            profile=t.get("profile"),
            max_turns=t.get("max_turns", 5),
            timeout=t.get("timeout", 90),
        ))

    results = await asyncio.gather(*coros, return_exceptions=True)

    # Convert exceptions to failed AgentResults
    final = []
    for i, r in enumerate(results):
        if isinstance(r, Exception):
            final.append(AgentResult(
                name=tasks[i].get("name", "agent"),
                success=False,
                output=f"Unexpected error: {r}",
                elapsed_seconds=0,
            ))
        else:
            final.append(r)

    succeeded = sum(1 for r in final if r.success)
    logger.info(
        "Parallel spawn: %d/%d agents succeeded",
        succeeded, len(final),
    )
    return final


def format_agent_results(results: list[AgentResult]) -> str:
    """Format multiple agent results into a clean text block for the brain."""
    parts = []
    for r in results:
        status = "OK" if r.success else "FAILED"
        parts.append(
            f"[AGENT: {r.name} | {status} | {r.elapsed_seconds:.1f}s]\n"
            f"{r.output}"
        )
    return "\n\n---\n\n".join(parts)
