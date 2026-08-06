"""Skills — agent-created procedural memory.

Lets the agent create, store, and retrieve reusable procedures.
Skills are markdown files stored in data/skills/ with frontmatter metadata.
Relevant skills are loaded into context based on query similarity.
"""

import logging
import re
from pathlib import Path
from datetime import datetime
from zoneinfo import ZoneInfo

from goodman.config import PROJECT_ROOT, TIMEZONE

log = logging.getLogger(__name__)

SKILLS_DIR = PROJECT_ROOT / "data" / "skills"
SKILLS_DIR.mkdir(parents=True, exist_ok=True)

MAX_SKILLS_IN_CONTEXT = 3
MAX_SKILL_SIZE = 2000  # chars per skill in context


def list_skills() -> list[dict]:
    """List all available skills with metadata."""
    skills = []
    for path in sorted(SKILLS_DIR.glob("*.md")):
        meta = _parse_frontmatter(path)
        meta["filename"] = path.name
        meta["size"] = path.stat().st_size
        skills.append(meta)
    return skills


def get_skill(name: str) -> str | None:
    """Get skill content by name (without .md extension)."""
    path = SKILLS_DIR / f"{name}.md"
    if path.exists():
        return path.read_text()
    # Try fuzzy match
    for p in SKILLS_DIR.glob("*.md"):
        if name.lower() in p.stem.lower():
            return p.read_text()
    return None


def save_skill(name: str, description: str, content: str,
               category: str = "general", tags: list[str] | None = None) -> str:
    """Save a new skill or update existing one.

    Returns the filename.
    """
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    path = SKILLS_DIR / f"{slug}.md"

    now = datetime.now(ZoneInfo(TIMEZONE))
    frontmatter = (
        f"---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"category: {category}\n"
        f"tags: {', '.join(tags or [])}\n"
        f"created: {now.strftime('%Y-%m-%d')}\n"
        f"updated: {now.strftime('%Y-%m-%d')}\n"
        f"---\n\n"
    )

    path.write_text(frontmatter + content)
    log.info("Saved skill: %s (%d chars)", slug, len(content))
    return path.name


def delete_skill(name: str) -> bool:
    """Delete a skill by name."""
    slug = re.sub(r'[^a-z0-9]+', '-', name.lower()).strip('-')
    path = SKILLS_DIR / f"{slug}.md"
    if path.exists():
        path.unlink()
        log.info("Deleted skill: %s", slug)
        return True
    return False


def get_relevant_skills(query: str, top_k: int = MAX_SKILLS_IN_CONTEXT) -> list[dict]:
    """Find skills relevant to a query using keyword matching.

    Returns list of dicts with name, description, content (truncated).
    """
    if not query or len(query.strip()) < 3:
        return []

    query_tokens = set(re.findall(r'[a-zA-Z0-9]+', query.lower()))
    if not query_tokens:
        return []

    scored = []
    for path in SKILLS_DIR.glob("*.md"):
        meta = _parse_frontmatter(path)
        content = path.read_text()

        # Score: tokens in name/description/tags/content that match query
        searchable = f"{meta.get('name', '')} {meta.get('description', '')} {meta.get('tags', '')} {content}"
        skill_tokens = set(re.findall(r'[a-zA-Z0-9]+', searchable.lower()))

        overlap = len(query_tokens & skill_tokens)
        if overlap < 2:
            continue

        score = overlap / max(len(query_tokens), 1)

        # Truncate content for context injection
        body = _strip_frontmatter(content)
        if len(body) > MAX_SKILL_SIZE:
            body = body[:MAX_SKILL_SIZE] + "\n[... truncated ...]"

        scored.append({
            "name": meta.get("name", path.stem),
            "description": meta.get("description", ""),
            "content": body,
            "score": score,
        })

    scored.sort(key=lambda x: -x["score"])
    return scored[:top_k]


def format_skills_for_context(query: str) -> str:
    """Format relevant skills for injection into the prompt context."""
    skills = get_relevant_skills(query)
    if not skills:
        return ""

    parts = ["=== RELEVANT SKILLS (reusable procedures) ==="]
    for s in skills:
        parts.append(f"\n### {s['name']}\n{s['description']}\n\n{s['content']}")

    return "\n".join(parts)


def format_skills_index() -> str:
    """Format a compact index of all skills for the system prompt."""
    skills = list_skills()
    if not skills:
        return ""

    lines = ["Available skills (use `skill view <name>` to read):"]
    for s in skills:
        desc = s.get("description", "")
        lines.append(f"  - {s.get('name', s['filename'])}: {desc}")

    return "\n".join(lines)


def _parse_frontmatter(path: Path) -> dict:
    """Parse YAML-like frontmatter from a skill file."""
    text = path.read_text()
    meta = {}
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            for line in text[3:end].strip().split("\n"):
                if ":" in line:
                    key, _, val = line.partition(":")
                    meta[key.strip()] = val.strip()
    return meta


def _strip_frontmatter(text: str) -> str:
    """Remove frontmatter from skill content."""
    if text.startswith("---"):
        end = text.find("---", 3)
        if end > 0:
            return text[end + 3:].strip()
    return text
