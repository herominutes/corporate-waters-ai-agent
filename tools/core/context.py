"""Context Assembly Pipeline — token-aware context budgeting.

Inspired by OpenClaw's context assembly architecture. Instead of concatenating
everything and hoping it fits, this module manages token budgets per section
and trims intelligently when over budget.

Sections (in priority order — last to be trimmed first):
  1. System prompt (soul files) — never trimmed
  2. Notebooks (running-state.md + on-demand) — always loaded
  3. Permanent memories — trimmed last (relevance-filtered by db.get_relevant_memories)
  4. Current session messages — trimmed from oldest
  5. Session summaries — trimmed from oldest
  6. Fetched data (email, calendar, web) — trimmed from longest
  7. Time context — never trimmed

Token estimation uses chars/4 as a rough proxy (avoids tiktoken dependency).
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from goodman.config import TIMEZONE

logger = logging.getLogger(__name__)


def _localize_ts(ts: str) -> str:
    """Convert a SQLite UTC timestamp ('YYYY-MM-DD HH:MM:SS') to local TIMEZONE.

    DB timestamps come from CURRENT_TIMESTAMP (UTC, no tz marker). The prompt's
    "Current time" is rendered in local TIMEZONE, so leaving these as UTC creates
    a phantom offset that makes the model miscompute elapsed time. Returns a clean
    local string; falls back to the original on parse failure.
    """
    if not ts:
        return ts
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00").replace(" ", "T"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=ZoneInfo("UTC"))
        return dt.astimezone(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d %H:%M")
    except (ValueError, TypeError):
        return ts


# Token budget configuration
# Claude's context window is ~200k tokens. We reserve generous headroom
# for the response and tool use overhead.
TOTAL_TOKEN_BUDGET = 100_000   # ~100k tokens for prompt
RESPONSE_RESERVE = 8_000      # Reserve for model response

# Per-section budgets (in estimated tokens)
SECTION_BUDGETS = {
    "system_prompt": 15_000,   # Soul files — generous, rarely trimmed
    "notebooks": 8_000,        # Always-on notebooks (running-state etc)
    "memories": 6_000,         # Permanent memories (personal, not docs)
    "session_messages": 12_000, # Current session conversation (trimmed from oldest); older context lives in session_summaries
    "session_summaries": 8_000, # Past session summaries
    "tasks": 3_000,            # Open task queue
    "fetched_data": 18_000,    # Email, calendar, web results
    "time_context": 500,       # Current time string
}


def estimate_tokens(text: str) -> int:
    """Rough token estimate: ~4 chars per token for English text."""
    return len(text) // 4 if text else 0


def _compact_graph_context(text: str) -> str:
    """Strip redundant tokens from knowledge graph context.

    Collapses multi-line entity blocks to single lines and removes
    empty properties. Caps at 3000 chars total.
    """
    if not text:
        return ""

    lines = text.split("\n")
    result_lines = []
    current_entity = None
    current_parts = []

    # Strip the header if the content is short (< 500 chars)
    header = ""
    body_lines = lines
    if lines and lines[0].startswith("=== KNOWLEDGE GRAPH"):
        header = lines[0]
        body_lines = lines[1:]
        # Only keep header if content is substantial
        if len(text) < 500:
            header = ""

    for line in body_lines:
        stripped = line.strip()
        if not stripped:
            continue

        # New entity line (not indented) — e.g. "Heiko Strubel (person)"
        if not line.startswith(" ") and not line.startswith("\t"):
            # Flush previous entity
            if current_entity is not None:
                if current_parts:
                    result_lines.append(f"{current_entity} {', '.join(current_parts)}")
                else:
                    result_lines.append(current_entity)
            current_entity = stripped
            current_parts = []
        elif current_entity is not None:
            # Indented line — property or relationship
            # Skip empty values like "  key: " or "  key:"
            if ": " in stripped:
                key, _, val = stripped.partition(": ")
                val = val.strip()
                if not val:
                    continue
            # Collapse into single-line parts
            current_parts.append(stripped)

    # Flush last entity
    if current_entity is not None:
        if current_parts:
            result_lines.append(f"{current_entity} {', '.join(current_parts)}")
        else:
            result_lines.append(current_entity)

    compact = "\n".join(result_lines)
    if header:
        compact = header + "\n" + compact

    # Cap at 3000 chars
    if len(compact) > 3000:
        compact = compact[:3000] + "\n[... graph context trimmed ...]"

    return compact


def _compact_memories(memories: list[dict]) -> list[dict]:
    """Compact memories by truncating large content.

    - Document-type memories: truncate to 200 chars + '[...]'
    - Other memories: cap at 500 chars each
    - Removes empty content
    """
    compacted = []
    for m in memories:
        content = m.get("content", "")
        category = m.get("category", "")

        if not content.strip():
            continue

        if category == "document":
            if len(content) > 200:
                content = content[:200] + " [...]"
        else:
            if len(content) > 500:
                content = content[:500] + " [...]"

        compacted.append({"category": category, "content": content})

    return compacted


_THINKING_RE = re.compile(
    r'(\[THINKING\].*?\[/THINKING\]|<thinking>.*?</thinking>)',
    re.DOTALL | re.IGNORECASE,
)


def _strip_reasoning(text: str) -> str:
    """Strip [THINKING]...[/THINKING] and <thinking>...</thinking> blocks from text."""
    return _THINKING_RE.sub('', text).strip()


class ContextBuilder:
    """Assembles context within token budgets.

    Usage:
        builder = ContextBuilder()
        builder.set_system_prompt(prompt)
        builder.set_memories(memories)
        builder.set_session_messages(messages)
        builder.set_session_summaries(summaries)
        builder.set_fetched_data(data_parts)
        full_prompt = builder.build(user_message)
    """

    def __init__(self, total_budget: int = TOTAL_TOKEN_BUDGET):
        self.total_budget = total_budget
        self._system_prompt: str = ""
        self._notebooks: str = ""
        self._memories: str = ""
        self._task_states: str = ""
        self._session_messages: str = ""
        self._session_summaries: str = ""
        self._tasks: str = ""
        self._prior_context: str = ""
        self._volatile_soul: str = ""
        self._skills: str = ""
        self._fetched_data: str = ""
        self._time_context: str = ""
        self._now_str: str = ""
        self._usage: dict[str, int] = {}

    def set_system_prompt(self, prompt: str):
        """Set the system prompt (soul files)."""
        self._system_prompt = prompt

    def set_volatile_soul(self, text: str):
        """Set volatile soul context (USER.md, MEMORY.md, daily logs).

        Kept in user prompt rather than system prompt for cache stability.
        """
        self._volatile_soul = text

    def set_skills(self, skills_text: str):
        """Set relevant skills context."""
        self._skills = skills_text

    def set_notebooks(self, running_state: str, notebook_list: list[str]):
        """Set always-on notebook context.

        running_state: contents of running-state.md (always loaded)
        notebook_list: names of all available notebooks (so brain knows what to look up)

        If running_state contains a knowledge graph section, it gets compacted
        via _compact_graph_context() to reduce token usage.
        """
        budget_chars = SECTION_BUDGETS["notebooks"] * 4
        parts = []

        if running_state:
            rs_text = running_state
            # Compact any embedded knowledge graph context
            kg_marker = "=== KNOWLEDGE GRAPH"
            if kg_marker in rs_text:
                before_kg, _, kg_and_after = rs_text.partition(kg_marker)
                # Find the end of the graph section (next === or end of text)
                next_section = kg_and_after.find("\n===", 1)
                if next_section > 0:
                    kg_section = kg_marker + kg_and_after[:next_section]
                    after_section = kg_and_after[next_section:]
                else:
                    kg_section = kg_marker + kg_and_after
                    after_section = ""
                compacted_kg = _compact_graph_context(kg_section)
                rs_text = before_kg + compacted_kg + after_section
            if len(rs_text) > budget_chars * 3 // 4:
                rs_text = rs_text[:budget_chars * 3 // 4] + "\n[... running state trimmed ...]"
            parts.append(f"=== RUNNING STATE ===\n{rs_text}")

        if notebook_list:
            other = [n for n in notebook_list if n != "running-state"]
            if other:
                parts.append(
                    "Available notebooks (use `notebook read <name>` to load): "
                    + ", ".join(other)
                )

        self._notebooks = "\n\n".join(parts) if parts else ""

    def set_memories(self, memories: list[dict]):
        """Set permanent memories, formatting them.

        Applies compaction to reduce token usage: document memories are
        truncated to 200 chars, others capped at 500 chars.
        """
        if not memories:
            self._memories = ""
            return

        # Compact before formatting
        memories = _compact_memories(memories)

        budget = SECTION_BUDGETS["memories"]
        lines = []
        total_chars = 0

        for m in memories:
            line = f"- [{m['category']}] {m['content']}"
            line_chars = len(line) + 1
            if total_chars + line_chars > budget * 4:  # Convert budget back to chars
                lines.append(f"[... {len(memories) - len(lines)} more memories omitted ...]")
                break
            lines.append(line)
            total_chars += line_chars

        self._memories = "=== PERMANENT MEMORIES ===\n" + "\n".join(lines)

    def set_task_states(self, task_states: list[dict]):
        """Set active task states — always included at full fidelity.

        Task states capture the parameters and progress of complex multi-item
        tasks. They are NEVER truncated because losing them causes the exact
        context-loss problem they were designed to prevent.
        """
        if not task_states:
            self._task_states = ""
            return

        lines = []
        for ts in task_states:
            content = ts.get("content", "")
            created = ts.get("created_at", "")
            lines.append(f"- [saved {created}] {content}")

        self._task_states = (
            "=== ACTIVE TASK STATE (resume from here on 'continue') ===\n"
            + "\n".join(lines)
        )

    def set_tasks(self, tasks_text: str):
        """Set open tasks context."""
        self._tasks = tasks_text

    def set_prior_context(self, prior_text: str):
        """Set prior conversation context (what Saul previously said about this topic)."""
        self._prior_context = prior_text

    def set_session_messages(self, messages: list[dict]):
        """Set current session messages, trimming oldest if over budget.

        Notifications (Slack/TG/WhatsApp events) are separated from real
        conversation to prevent the LLM from confusing them with actual
        user messages or echoing them back as fabricated dialog.
        """
        if not messages:
            self._session_messages = ""
            return

        # Strip reasoning blocks from all assistant messages except the most recent one.
        # This frees up context space without losing the latest chain-of-thought.
        last_assistant_idx = None
        for i in range(len(messages) - 1, -1, -1):
            if messages[i]["role"] == "assistant":
                last_assistant_idx = i
                break
        for i, msg in enumerate(messages):
            if msg["role"] == "assistant" and i != last_assistant_idx:
                stripped = _strip_reasoning(msg["content"])
                if stripped != msg["content"]:
                    messages[i] = {**msg, "content": stripped}

        budget_chars = SECTION_BUDGETS["session_messages"] * 4

        # Separate real conversation from notifications
        conv_lines = []
        notif_lines = []
        total_chars = 0

        for msg in reversed(messages):
            content = msg["content"]
            timestamp = _localize_ts(msg.get("created_at", ""))

            # Detect notifications — they start with [NOTIFICATION
            if content.lstrip().startswith("[NOTIFICATION"):
                # Extract just the key info, skip the framing tags
                notif_lines.append(f"  {timestamp}: {content}")
                continue

            prefix = "User" if msg["role"] == "user" else "Saul"
            line = f"[{timestamp}] {prefix}: {content}"
            line_chars = len(line) + 1

            if total_chars + line_chars > budget_chars:
                conv_lines.append("[... older messages trimmed for context ...]")
                break
            conv_lines.append(line)
            total_chars += line_chars

        conv_lines.reverse()
        notif_lines.reverse()

        parts = ["=== RECENT CONVERSATION ===\n" + "\n".join(conv_lines)]

        # Add notifications as a clearly separate, compact section
        if notif_lines:
            # Keep only last 5 notifications and cap total size
            recent_notifs = notif_lines[-5:]
            notif_block = "\n".join(recent_notifs)
            if len(notif_block) > 2000:
                notif_block = notif_block[:2000] + "\n[... trimmed ...]"
            parts.append(
                "=== RECENT PLATFORM NOTIFICATIONS (not conversation — do NOT echo these) ===\n"
                + notif_block
            )

        self._session_messages = "\n\n".join(parts)

    def set_session_summaries(self, summaries: list[dict] | list[str]):
        """Set past session summaries."""
        if not summaries:
            self._session_summaries = ""
            return

        budget_chars = SECTION_BUDGETS["session_summaries"] * 4
        parts = []
        total_chars = 0

        for s in summaries:
            if isinstance(s, dict):
                text = s.get("summary", "")
                started = s.get("started_at", "")
                part = f"[Session from {started}]\n{text}"
            else:
                part = str(s)

            if total_chars + len(part) > budget_chars:
                break
            parts.append(part)
            total_chars += len(part)

        self._session_summaries = (
            "=== PAST CONVERSATION SUMMARIES ===\n" + "\n---\n".join(parts)
        )

    def set_fetched_data(self, data_parts: list[str]):
        """Set fetched data (email, calendar, web), trimming if over budget."""
        if not data_parts:
            self._fetched_data = ""
            return

        budget_chars = SECTION_BUDGETS["fetched_data"] * 4
        total_chars = sum(len(p) for p in data_parts)

        if total_chars <= budget_chars:
            self._fetched_data = "\n\n".join(data_parts)
            return

        # Over budget — trim the longest parts first
        # Sort by length (longest first) and trim proportionally
        indexed = [(i, len(p), p) for i, p in enumerate(data_parts)]
        indexed.sort(key=lambda x: -x[1])

        per_part_budget = budget_chars // len(data_parts)
        result = [None] * len(data_parts)

        remaining_budget = budget_chars
        for i, length, part in indexed:
            if length <= per_part_budget:
                result[i] = part
                remaining_budget -= length
            else:
                # Trim this part
                trimmed = part[:per_part_budget]
                # Try to cut at a good boundary
                for sep in ["\n\n", "\n", ". "]:
                    last = trimmed.rfind(sep)
                    if last > per_part_budget * 0.7:
                        trimmed = trimmed[:last + len(sep)]
                        break
                trimmed += f"\n[... trimmed, {length - len(trimmed)} chars omitted ...]"
                result[i] = trimmed
                remaining_budget -= len(trimmed)

        self._fetched_data = "\n\n".join(r for r in result if r)

    def _rebuild_user_prompt(self, user_message: str, is_heartbeat: bool = False) -> str:
        """Build (or rebuild) the user prompt from current section state.

        Extracted as a method so overflow prevention can re-call it after trimming.
        """
        # Each section is XML-wrapped so the LLM clearly distinguishes
        # reference material from the actual user request.
        context_parts = []

        if self._volatile_soul:
            context_parts.append(
                f"<soul-context>\n{self._volatile_soul}\n</soul-context>"
            )

        if self._notebooks:
            context_parts.append(
                f"<agent-state>\n{self._notebooks}\n</agent-state>"
            )

        if self._memories:
            context_parts.append(
                f"<memories>\n{self._memories}\n</memories>"
            )

        if self._skills:
            context_parts.append(
                f"<skills>\n{self._skills}\n</skills>"
            )

        if self._task_states:
            context_parts.append(
                f"<task-state>\n{self._task_states}\n</task-state>"
            )

        if self._tasks:
            context_parts.append(
                f"<tasks>\n{self._tasks}\n</tasks>"
            )

        if self._prior_context:
            context_parts.append(
                f"<prior-context>\n{self._prior_context}\n</prior-context>"
            )

        if not is_heartbeat:
            if self._session_summaries:
                context_parts.append(
                    f"<session-history>\n{self._session_summaries}\n</session-history>"
                )
            if self._session_messages:
                context_parts.append(
                    f"<conversation note=\"timestamps are {TIMEZONE} local time\">\n"
                    f"{self._session_messages}\n</conversation>"
                )

        context_block = "\n\n".join(context_parts)

        if is_heartbeat:
            return f"{self._time_context}\n\n{context_block}\n\n---\n{user_message}"
        else:
            return (
                f"{self._time_context}\n\n{context_block}\n\n"
                f"<instructions>\n"
                f"The XML sections above are reference context — use them naturally "
                f"if relevant but do NOT reproduce their contents verbatim. Never output "
                f"timestamps or \"User:\"/\"Saul:\" prefixes from conversation history.\n"
                f"\n"
                f"TIME AWARENESS: The current time is shown at the top of this prompt and "
                f"on the <user-message> below; conversation timestamps are in the same "
                f"local timezone. Be naturally time-aware — greet appropriately for the "
                f"time of day, and acknowledge the gap since the last message when it's "
                f"notable (e.g. 'morning', 'been a while'). Reason about elapsed time from "
                f"these timestamps; do NOT guess the time or echo raw timestamps.\n"
                f"\n"
                f"TOOL-USE RULE: You have limited tool turns. After 3-4 tool calls, "
                f"produce a text response with what you found so far, even if incomplete. "
                f"The user sees NOTHING if your last turn is a tool call with no text. "
                f"Always end with a text message.\n"
                f"\n"
                f"TASK STATE RULE: When working on a multi-item task (drafting messages "
                f"for multiple people, processing a list, batch operations), emit a "
                f"[TASK_STATE: ...] directive at the START of your response with the full "
                f"task parameters. This persists across context loss so 'continue' works. "
                f"Format: [TASK_STATE: {{\"task\": \"description\", \"filter\": \"criteria used\", "
                f"\"items\": [\"item1\", \"item2\", ...], \"completed\": [\"done1\"], "
                f"\"remaining\": [\"todo1\", \"todo2\"], \"data\": {{key details}}}}]\n"
                f"Update the task state as you make progress. Include ALL data needed to "
                f"resume: names, handles, filters, templates, any parameters. When the task "
                f"is finished, emit [TASK_STATE: {{\"task\": \"...\", \"status\": \"done\"}}].\n"
                f"If a <task-state> section exists above, resume from where it left off.\n"
                f"</instructions>\n\n"
                f"<user-message received=\"{self._now_str}\">\n{user_message}\n</user-message>"
            )

    def build(self, user_message: str, is_heartbeat: bool = False) -> tuple[str, str]:
        """Build the final system prompt and user prompt.

        Returns (system_prompt, full_user_prompt).
        """
        # Time context
        now = datetime.now(ZoneInfo(TIMEZONE))
        self._time_context = f"Current time: {now.strftime('%A, %B %d %Y, %I:%M %p %Z')}"
        self._now_str = now.strftime("%A, %B %d %Y, %I:%M %p %Z")

        # Build the user prompt from context sections.
        full_prompt = self._rebuild_user_prompt(user_message, is_heartbeat)

        # Log context usage
        self._usage = {
            "system_prompt": estimate_tokens(self._system_prompt),
            "volatile_soul": estimate_tokens(self._volatile_soul),
            "skills": estimate_tokens(self._skills),
            "notebooks": estimate_tokens(self._notebooks),
            "memories": estimate_tokens(self._memories),
            "task_states": estimate_tokens(self._task_states),
            "tasks": estimate_tokens(self._tasks),
            "prior_context": estimate_tokens(self._prior_context),
            "session_messages": estimate_tokens(self._session_messages),
            "session_summaries": estimate_tokens(self._session_summaries),
            "fetched_data": estimate_tokens(self._fetched_data),
            "user_prompt": estimate_tokens(full_prompt),
        }
        total = sum(self._usage.values())
        logger.debug(
            "Context assembled: %d est. tokens — %s",
            total,
            ", ".join(f"{k}={v}" for k, v in self._usage.items()),
        )

        if total > self.total_budget:
            logger.warning(
                "Context over budget: %d / %d tokens. Consider reducing section sizes.",
                total, self.total_budget,
            )

        # Context overflow prevention: if >180K tokens (90% of 200K window),
        # aggressively trim sections to avoid API context overflow errors.
        OVERFLOW_THRESHOLD = 180_000
        total_est = estimate_tokens(self._system_prompt) + estimate_tokens(full_prompt)

        if total_est > OVERFLOW_THRESHOLD:
            logger.warning(
                "Context overflow prevention triggered: %d tokens > %d threshold",
                total_est, OVERFLOW_THRESHOLD,
            )

            # Step 1: Cut session_messages to half
            if self._session_messages:
                half = len(self._session_messages) // 2
                cut = self._session_messages[:half]
                # Find a clean break
                last_nl = cut.rfind("\n")
                if last_nl > half * 0.5:
                    cut = cut[:last_nl]
                self._session_messages = cut + "\n[... messages trimmed for context limit ...]"
                logger.warning("Overflow trim step 1: session_messages cut to half")

            # Rebuild prompt after trim
            full_prompt = self._rebuild_user_prompt(user_message, is_heartbeat)
            total_est = estimate_tokens(self._system_prompt) + estimate_tokens(full_prompt)

            if total_est > OVERFLOW_THRESHOLD:
                # Step 2: Cut session_summaries entirely
                self._session_summaries = ""
                logger.warning("Overflow trim step 2: session_summaries removed")
                full_prompt = self._rebuild_user_prompt(user_message, is_heartbeat)
                total_est = estimate_tokens(self._system_prompt) + estimate_tokens(full_prompt)

            if total_est > OVERFLOW_THRESHOLD:
                # Step 3: Cut memories to top 5 lines
                if self._memories:
                    lines = self._memories.split("\n")
                    # Keep header + first 5 memory lines
                    self._memories = "\n".join(lines[:6]) + "\n[... memories trimmed for context limit ...]"
                    logger.warning("Overflow trim step 3: memories cut to top 5")
                    full_prompt = self._rebuild_user_prompt(user_message, is_heartbeat)

        return self._system_prompt, full_prompt

    @property
    def usage(self) -> dict[str, int]:
        """Token usage breakdown from the last build() call."""
        return self._usage
