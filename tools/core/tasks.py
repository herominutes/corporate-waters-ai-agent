"""Task queue manager — persistent tasks that survive across sessions.

When the LLM can't complete something in one session, it saves a task.
The heartbeat reviews blocked tasks and proactively suggests alternatives.
"""

import logging
import re

from goodman import db, soul

log = logging.getLogger(__name__)

# Directive patterns
_TASK_RE = re.compile(r'^\[TASK:(?:([a-z]+):\s*)?(.+?)\]$')
_TASK_DONE_RE = re.compile(r'^\[TASK_DONE:\s*(.+?)\]$')
_TASK_BLOCKED_RE = re.compile(r'^\[TASK_BLOCKED:\s*(.+?)\]$')


async def extract_task_directives(user_id: str, response: str):
    """Parse and execute task directives from LLM response."""
    for line in response.split("\n"):
        line = line.strip()

        # [TASK: description] or [TASK:high: description]
        m = _TASK_RE.match(line)
        if m:
            priority = m.group(1) if m.group(1) in ("high", "normal", "low") else "normal"
            description = m.group(2).strip()
            if description:
                task_id = await db.create_task(user_id, description, priority)
                if task_id:
                    soul.append_daily_log(f"[TASK CREATED] #{task_id}: {description[:100]}")
                    log.info("Task created #%d: %s", task_id, description[:80])
            continue

        # [TASK_DONE: description]
        m = _TASK_DONE_RE.match(line)
        if m:
            query = m.group(1).strip()
            task = await db.find_task_by_description(user_id, query)
            if task:
                await db.complete_task(task["id"])
                soul.append_daily_log(f"[TASK DONE] #{task['id']}: {task['description'][:100]}")
                log.info("Task completed #%d: %s", task["id"], task["description"][:80])
            else:
                log.debug("TASK_DONE: no matching task for '%s'", query[:60])
            continue

        # [TASK_BLOCKED: description | reason: why | next: alternative]
        m = _TASK_BLOCKED_RE.match(line)
        if m:
            raw = m.group(1).strip()
            parts = [p.strip() for p in raw.split("|")]
            query = parts[0]
            reason = ""
            next_approach = ""
            for part in parts[1:]:
                if part.lower().startswith("reason:"):
                    reason = part[7:].strip()
                elif part.lower().startswith("next:"):
                    next_approach = part[5:].strip()

            task = await db.find_task_by_description(user_id, query)
            if task:
                await db.mark_task_blocked(task["id"], reason, next_approach)
                soul.append_daily_log(
                    f"[TASK BLOCKED] #{task['id']}: {task['description'][:60]} — {reason[:60]}"
                )
                log.info("Task blocked #%d: %s", task["id"], reason[:80])
            else:
                # Task doesn't exist yet — create it as blocked
                task_id = await db.create_task(user_id, query, "normal")
                if task_id:
                    await db.mark_task_blocked(task_id, reason, next_approach)
                    soul.append_daily_log(f"[TASK CREATED+BLOCKED] #{task_id}: {query[:60]}")
            continue


async def format_tasks_for_context(user_id: str) -> str:
    """Format open tasks as compact text for LLM context injection."""
    tasks = await db.get_all_open_tasks(user_id)
    if not tasks:
        return ""

    lines = ["=== OPEN TASKS ==="]
    for t in tasks:
        priority_tag = f"[{t['priority'].upper()}] " if t['priority'] != 'normal' else ""
        status_info = t["status"]
        if t["attempts_count"] > 0:
            status_info += f", {t['attempts_count']} attempts"
        if t["failure_reason"]:
            status_info += f", last fail: {t['failure_reason'][:60]}"
        if t["next_approach"]:
            status_info += f", next: {t['next_approach'][:60]}"

        lines.append(f"#{t['id']} {priority_tag}{t['description'][:120]} ({status_info})")

    return "\n".join(lines)


async def format_blocked_for_review(user_id: str) -> str:
    """Format blocked tasks for heartbeat review — more detail than context version."""
    tasks = await db.get_blocked_tasks(user_id)
    if not tasks:
        return ""

    lines = ["BLOCKED TASKS NEEDING REVIEW:"]
    for t in tasks:
        lines.append(f"\n#{t['id']}: {t['description']}")
        lines.append(f"  Priority: {t['priority']}")
        lines.append(f"  Attempts: {t['attempts_count']}")
        if t["failure_reason"]:
            lines.append(f"  Last failure: {t['failure_reason']}")
        if t["next_approach"]:
            lines.append(f"  Suggested next: {t['next_approach']}")
        lines.append(f"  Created: {t['created_at']}")

    return "\n".join(lines)
