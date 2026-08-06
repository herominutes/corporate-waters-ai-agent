"""Notebooks — flexible file-based persistent documents.

Named markdown files that Saul manages himself. Unlike vector memories
(retrieved by semantic search), notebooks are loaded by name — no fuzzy
matching, no relevance scoring. Think of them as labeled tabs in a
physical notebook.

Use cases:
  - Spending tracker (format can change any time)
  - LinkedIn metrics & post history
  - Draft messages awaiting approval
  - Running state (cash position, daily budget, open loops)

CLI interface (used by Claude subprocess):
  notebook list                      List all notebooks
  notebook read <name>               Read a notebook's contents
  notebook write <name> <content>    Overwrite a notebook
  notebook append <name> <content>   Append to a notebook
"""

import re
import sys
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
NOTEBOOKS_DIR = PROJ_ROOT / "data" / "notebooks"
NOTEBOOKS_DIR.mkdir(parents=True, exist_ok=True)

# Max notebook size to prevent context bloat
MAX_NOTEBOOK_SIZE = 50_000  # ~12.5k tokens


def _sanitize_name(name: str) -> str:
    """Sanitize notebook name to a safe filename."""
    # Strip .md extension if provided
    name = name.removesuffix(".md")
    # Only allow alphanumeric, hyphens, underscores
    name = re.sub(r'[^a-zA-Z0-9_-]', '-', name)
    # Collapse multiple hyphens
    name = re.sub(r'-+', '-', name).strip('-')
    if not name:
        raise ValueError("Invalid notebook name")
    return name


def _path(name: str) -> Path:
    """Get the file path for a notebook."""
    return NOTEBOOKS_DIR / f"{_sanitize_name(name)}.md"


def list_notebooks() -> list[str]:
    """List all notebook names (without .md extension)."""
    notebooks = []
    for f in sorted(NOTEBOOKS_DIR.glob("*.md")):
        notebooks.append(f.stem)
    return notebooks


def read_notebook(name: str) -> str:
    """Read a notebook's contents. Returns empty string if not found."""
    path = _path(name)
    if not path.exists():
        return ""
    return path.read_text(encoding="utf-8")


def write_notebook(name: str, content: str) -> str:
    """Write/overwrite a notebook. Returns confirmation."""
    if len(content) > MAX_NOTEBOOK_SIZE:
        return f"Error: content too large ({len(content)} chars, max {MAX_NOTEBOOK_SIZE})"
    path = _path(name)
    path.write_text(content, encoding="utf-8")
    return f"Notebook '{name}' saved ({len(content)} chars)"


def append_notebook(name: str, content: str) -> str:
    """Append content to a notebook, creating it if needed."""
    path = _path(name)
    existing = path.read_text(encoding="utf-8") if path.exists() else ""
    new_content = f"{existing}\n{content}" if existing else content
    if len(new_content) > MAX_NOTEBOOK_SIZE:
        return f"Error: notebook would exceed max size ({len(new_content)} chars, max {MAX_NOTEBOOK_SIZE})"
    path.write_text(new_content, encoding="utf-8")
    return f"Appended to '{name}' ({len(content)} chars added, {len(new_content)} total)"


def _cli():
    """CLI entry point for subprocess tool use."""
    args = sys.argv[1:]
    if not args:
        print("Usage: notebook list|read|write|append [name] [content]")
        sys.exit(1)

    cmd = args[0].lower()

    if cmd == "list":
        notebooks = list_notebooks()
        if notebooks:
            for n in notebooks:
                size = _path(n).stat().st_size
                print(f"  {n} ({size:,} chars)")
        else:
            print("No notebooks yet.")

    elif cmd == "read":
        if len(args) < 2:
            print("Usage: notebook read <name>")
            sys.exit(1)
        content = read_notebook(args[1])
        if content:
            print(content)
        else:
            print(f"Notebook '{args[1]}' not found or empty.")

    elif cmd == "write":
        if len(args) < 3:
            print("Usage: notebook write <name> <content>")
            sys.exit(1)
        content = " ".join(args[2:])
        print(write_notebook(args[1], content))

    elif cmd == "append":
        if len(args) < 3:
            print("Usage: notebook append <name> <content>")
            sys.exit(1)
        content = " ".join(args[2:])
        print(append_notebook(args[1], content))

    else:
        print(f"Unknown command: {cmd}")
        print("Commands: list, read, write, append")
        sys.exit(1)


if __name__ == "__main__":
    _cli()
