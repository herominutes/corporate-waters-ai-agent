"""Confluence integration for The Goodman Protocol.

Fetches and caches Confluence pages via Atlassian Cloud REST API v2.
Pages are stored in SQLite for instant recall. Links detected in
Slack messages are auto-fetched and indexed.
"""

import base64
import logging
import re
import time

import httpx

from goodman.config import (
    CONFLUENCE_BASE_URL,
    CONFLUENCE_EMAIL,
    CONFLUENCE_API_TOKEN,
    CONFLUENCE_DEFAULT_SPACE,
)

logger = logging.getLogger(__name__)

_CONFLUENCE_URL_RE = re.compile(
    r'https?://[a-zA-Z0-9.-]+\.atlassian\.net/wiki/'
    r'(?:spaces/\w+/pages/(\d+)|pages/(\d+))'
)

_CACHE_TTL = 3600


def _auth_header() -> dict[str, str]:
    cred = base64.b64encode(f"{CONFLUENCE_EMAIL}:{CONFLUENCE_API_TOKEN}".encode()).decode()
    return {"Authorization": f"Basic {cred}", "Accept": "application/json"}


def enabled() -> bool:
    return bool(CONFLUENCE_BASE_URL and CONFLUENCE_EMAIL and CONFLUENCE_API_TOKEN)


def extract_page_ids(text: str) -> list[str]:
    """Extract Confluence page IDs from URLs in text."""
    ids = []
    for m in _CONFLUENCE_URL_RE.finditer(text):
        pid = m.group(1) or m.group(2)
        if pid:
            ids.append(pid)
    return ids


def extract_urls(text: str) -> list[str]:
    """Extract full Confluence URLs from text."""
    return _CONFLUENCE_URL_RE.findall(text)


def fetch_page(page_id: str) -> dict | None:
    """Fetch a single Confluence page by ID. Returns dict with title, body, url, space."""
    if not enabled():
        return None
    url = f"{CONFLUENCE_BASE_URL}/wiki/api/v2/pages/{page_id}"
    params = {"body-format": "storage"}
    try:
        resp = httpx.get(url, headers=_auth_header(), params=params, timeout=15)
        if resp.status_code == 404:
            logger.warning("Confluence page %s not found", page_id)
            return None
        resp.raise_for_status()
        data = resp.json()
        title = data.get("title", "")
        space_id = data.get("spaceId", "")
        body_html = data.get("body", {}).get("storage", {}).get("value", "")
        page_url = f"{CONFLUENCE_BASE_URL}/wiki/spaces/{CONFLUENCE_DEFAULT_SPACE}/pages/{page_id}"
        body_text = _html_to_text(body_html)
        return {
            "page_id": page_id,
            "title": title,
            "body": body_text,
            "body_html": body_html,
            "url": page_url,
            "space_id": space_id,
            "fetched_at": time.time(),
        }
    except Exception as e:
        logger.error("Confluence fetch error for page %s: %s", page_id, e)
        return None


def fetch_page_by_url(url: str) -> dict | None:
    """Fetch a Confluence page from its full URL."""
    ids = extract_page_ids(url)
    if not ids:
        return None
    return fetch_page(ids[0])


def search_pages(query: str, space: str | None = None, limit: int = 10) -> list[dict]:
    """Search Confluence pages by CQL query."""
    if not enabled():
        return []
    space = space or CONFLUENCE_DEFAULT_SPACE
    cql = f'space = "{space}" AND (title ~ "{query}" OR text ~ "{query}")'
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content/search"
    params = {"cql": cql, "limit": limit, "expand": "body.storage"}
    try:
        resp = httpx.get(url, headers=_auth_header(), params=params, timeout=15)
        resp.raise_for_status()
        results = []
        for r in resp.json().get("results", []):
            body_html = r.get("body", {}).get("storage", {}).get("value", "")
            page_id = r.get("id", "")
            results.append({
                "page_id": page_id,
                "title": r.get("title", ""),
                "body": _html_to_text(body_html)[:3000],
                "url": f"{CONFLUENCE_BASE_URL}/wiki/spaces/{space}/pages/{page_id}",
                "fetched_at": time.time(),
            })
        return results
    except Exception as e:
        logger.error("Confluence search error: %s", e)
        return []


def get_space_pages(space: str | None = None, limit: int = 50) -> list[dict]:
    """List pages in a Confluence space."""
    if not enabled():
        return []
    space = space or CONFLUENCE_DEFAULT_SPACE
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content"
    params = {"spaceKey": space, "limit": limit, "type": "page", "expand": "version"}
    try:
        resp = httpx.get(url, headers=_auth_header(), params=params, timeout=15)
        resp.raise_for_status()
        pages = []
        for r in resp.json().get("results", []):
            pages.append({
                "page_id": r.get("id", ""),
                "title": r.get("title", ""),
                "url": f"{CONFLUENCE_BASE_URL}/wiki/spaces/{space}/pages/{r.get('id', '')}",
                "version": r.get("version", {}).get("number", 0),
            })
        return pages
    except Exception as e:
        logger.error("Confluence get_space_pages error: %s", e)
        return []


def get_space_tree(space: str | None = None) -> list[dict]:
    """Get the full page tree of a space (titles + hierarchy). Used for relevance checking."""
    if not enabled():
        return []
    space = space or CONFLUENCE_DEFAULT_SPACE
    url = f"{CONFLUENCE_BASE_URL}/wiki/rest/api/content"
    params = {"spaceKey": space, "limit": 100, "type": "page",
              "expand": "ancestors"}
    try:
        resp = httpx.get(url, headers=_auth_header(), params=params, timeout=15)
        resp.raise_for_status()
        tree = []
        for r in resp.json().get("results", []):
            ancestors = r.get("ancestors", [])
            parent_title = ancestors[-1]["title"] if ancestors else None
            tree.append({
                "page_id": r.get("id", ""),
                "title": r.get("title", ""),
                "parent": parent_title,
                "depth": len(ancestors),
            })
        return tree
    except Exception as e:
        logger.error("Confluence get_space_tree error: %s", e)
        return []


def get_space_structure_summary(space: str | None = None) -> str:
    """Human-readable summary of the space structure for relevance checking."""
    tree = get_space_tree(space)
    if not tree:
        return "Could not fetch space structure."
    lines = [f"Confluence space '{space or CONFLUENCE_DEFAULT_SPACE}' sections:"]
    for p in sorted(tree, key=lambda x: (x["depth"], x["title"])):
        indent = "  " * p["depth"]
        parent = f" (under: {p['parent']})" if p["parent"] else ""
        lines.append(f"{indent}- {p['title']}{parent} [id:{p['page_id']}]")
    return "\n".join(lines)


def find_page_by_title(title: str, space: str | None = None) -> dict | None:
    """Resolve a page title to {page_id, title} for use as a parent.

    Exact (case-insensitive) match first, then a contains-match either way,
    then a CQL search fallback. Returns None if nothing matches.
    """
    space = space or CONFLUENCE_DEFAULT_SPACE
    tnorm = title.strip().lower()
    tree = get_space_tree(space)
    if tree:
        for p in tree:
            if p["title"].strip().lower() == tnorm:
                return {"page_id": p["page_id"], "title": p["title"]}
        for p in tree:
            pt = p["title"].strip().lower()
            if tnorm and (tnorm in pt or pt in tnorm):
                return {"page_id": p["page_id"], "title": p["title"]}
    results = search_pages(title, space=space, limit=5)
    if results:
        return {"page_id": results[0]["page_id"], "title": results[0]["title"]}
    return None


def _text_to_storage(markdown_text: str) -> str:
    """Convert markdown/plain text to Confluence storage format HTML.

    Confluence storage macros (<ac:structured-macro> panels, info/note/code
    blocks, etc.) are preserved verbatim. The markdown renderer would otherwise
    escape the namespaced ac:/ri: tags into literal text (e.g. &lt;ac:...&gt;),
    which Confluence then displays as raw tags on the page. So we stash the
    macro blocks behind placeholders, render the surrounding markdown, then
    re-inject the macros untouched.
    """
    from markdown_it import MarkdownIt

    text = markdown_text.strip()

    # Stash Confluence storage macros so markdown can't escape them.
    stash: list[str] = []

    def _hold(m):
        stash.append(m.group(0))
        return f"\n\nMACROSTASH{len(stash) - 1}END\n\n"

    macro_re = re.compile(
        r'<ac:structured-macro\b.*?</ac:structured-macro>|<ac:[\w-]+\b[^>]*/>',
        re.DOTALL,
    )
    text = macro_re.sub(_hold, text)

    md = MarkdownIt("commonmark", {"html": True, "breaks": False}).enable("table")
    html = md.render(text)
    html = re.sub(r'\n{3,}', '\n\n', html)
    html = re.sub(r'<p>\s*</p>', '', html)

    # Re-inject macros (markdown may have wrapped the placeholder in <p>...</p>).
    for i, raw in enumerate(stash):
        token = f"MACROSTASH{i}END"
        html = html.replace(f"<p>{token}</p>", raw).replace(token, raw)

    return html.strip()


def create_page(title: str, body: str, parent_id: str | None = None,
                space: str | None = None) -> dict | None:
    """Create a new Confluence page. Body can be markdown or plain text."""
    import os
    if os.getenv("SLACK_CALLER_ROLE"):
        from goodman import config as _cfg
        if not _cfg.caller_has(_cfg.CAP_CONFLUENCE):
            return None
    if not enabled():
        return None
    space_key = space or CONFLUENCE_DEFAULT_SPACE
    # Need the space ID (numeric) for API v2
    space_id = _get_space_id(space_key)
    if not space_id:
        logger.error("Could not resolve space key %s to ID", space_key)
        return None
    storage_body = _text_to_storage(body)
    payload = {
        "spaceId": space_id,
        "status": "current",
        "title": title,
        "body": {
            "representation": "storage",
            "value": storage_body,
        },
    }
    if parent_id:
        payload["parentId"] = parent_id
    try:
        resp = httpx.post(
            f"{CONFLUENCE_BASE_URL}/wiki/api/v2/pages",
            headers={**_auth_header(), "Content-Type": "application/json"},
            json=payload, timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
        page_id = data.get("id", "")
        page_url = f"{CONFLUENCE_BASE_URL}/wiki/spaces/{space_key}/pages/{page_id}"
        logger.info("Created Confluence page: %s — %s", page_id, title)
        return {
            "page_id": page_id,
            "title": title,
            "url": page_url,
            "space_id": space_id,
        }
    except Exception as e:
        logger.error("Confluence create_page error: %s", e)
        return None


def update_page(page_id: str, title: str | None = None,
                body: str | None = None) -> dict | None:
    """Update an existing Confluence page. Increments version automatically."""
    import os
    if os.getenv("SLACK_CALLER_ROLE"):
        from goodman import config as _cfg
        if not _cfg.caller_has(_cfg.CAP_CONFLUENCE):
            return None
    if not enabled():
        return None
    try:
        current = httpx.get(
            f"{CONFLUENCE_BASE_URL}/wiki/api/v2/pages/{page_id}",
            headers=_auth_header(), params={"body-format": "storage"}, timeout=15,
        )
        current.raise_for_status()
        data = current.json()
        current_version = data.get("version", {}).get("number", 1)
        current_title = data.get("title", "")
        payload = {
            "id": page_id,
            "status": "current",
            "title": title or current_title,
            "version": {"number": current_version + 1},
        }
        if body is not None:
            payload["body"] = {
                "representation": "storage",
                "value": _text_to_storage(body),
            }
        resp = httpx.put(
            f"{CONFLUENCE_BASE_URL}/wiki/api/v2/pages/{page_id}",
            headers={**_auth_header(), "Content-Type": "application/json"},
            json=payload, timeout=15,
        )
        resp.raise_for_status()
        result = resp.json()
        pid = result.get("id", page_id)
        logger.info("Updated Confluence page: %s — %s", pid, payload["title"])
        return {
            "page_id": pid,
            "title": payload["title"],
            "url": f"{CONFLUENCE_BASE_URL}/wiki/spaces/{CONFLUENCE_DEFAULT_SPACE}/pages/{pid}",
            "version": current_version + 1,
        }
    except Exception as e:
        logger.error("Confluence update_page error: %s", e)
        return None


_space_id_cache: dict[str, str] = {}


def _get_space_id(space_key: str) -> str | None:
    """Resolve a space key (e.g. 'PS') to a numeric space ID."""
    if space_key in _space_id_cache:
        return _space_id_cache[space_key]
    try:
        resp = httpx.get(
            f"{CONFLUENCE_BASE_URL}/wiki/rest/api/space/{space_key}",
            headers=_auth_header(), timeout=10,
        )
        resp.raise_for_status()
        sid = str(resp.json().get("id", ""))
        if sid:
            _space_id_cache[space_key] = sid
        return sid or None
    except Exception as e:
        logger.error("Confluence _get_space_id error for %s: %s", space_key, e)
        return None


def _html_to_text(html: str) -> str:
    """Convert Confluence storage format HTML to readable text."""
    try:
        import html2text
        h = html2text.HTML2Text()
        h.body_width = 0
        h.ignore_images = True
        h.ignore_links = False
        h.inline_links = True
        h.skip_internal_links = True
        return h.handle(html).strip()
    except ImportError:
        pass
    text = re.sub(r'<[^>]+>', ' ', html)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    return re.sub(r'\s+', ' ', text).strip()


# -------------------------------------------------------------------
# SQLite cache layer (sync — used from threaded Slack callbacks)
# -------------------------------------------------------------------

import sqlite3
import threading

_local = threading.local()
_db_ready = False


def _get_sync_db() -> sqlite3.Connection:
    """Thread-local sync SQLite connection for the Confluence cache."""
    if not hasattr(_local, "conn") or _local.conn is None:
        from goodman.config import DATA_DIR
        db_path = DATA_DIR / "goodman.db"
        _local.conn = sqlite3.connect(str(db_path), timeout=10)
        _local.conn.execute("PRAGMA journal_mode=WAL")
    return _local.conn


def _ensure_table():
    global _db_ready
    if _db_ready:
        return
    db = _get_sync_db()
    db.execute("""
        CREATE TABLE IF NOT EXISTS confluence_pages (
            page_id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT NOT NULL,
            url TEXT NOT NULL,
            space_id TEXT,
            fetched_at REAL NOT NULL
        )
    """)
    db.commit()
    _db_ready = True


def cache_page(page: dict):
    """Store or update a page in the local cache."""
    _ensure_table()
    db = _get_sync_db()
    db.execute(
        "INSERT OR REPLACE INTO confluence_pages (page_id, title, body, url, space_id, fetched_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (page["page_id"], page["title"], page["body"], page["url"],
         page.get("space_id", ""), page["fetched_at"]),
    )
    db.commit()


def get_cached_page(page_id: str, max_age: float = _CACHE_TTL) -> dict | None:
    """Retrieve a page from cache if fresh enough."""
    _ensure_table()
    db = _get_sync_db()
    row = db.execute(
        "SELECT page_id, title, body, url, space_id, fetched_at FROM confluence_pages WHERE page_id = ?",
        (page_id,),
    ).fetchone()
    if not row:
        return None
    if max_age > 0 and (time.time() - row[5]) > max_age:
        return None
    return {"page_id": row[0], "title": row[1], "body": row[2],
            "url": row[3], "space_id": row[4], "fetched_at": row[5]}


def search_cached(query: str, limit: int = 5) -> list[dict]:
    """Search cached Confluence pages by title or body content."""
    _ensure_table()
    db = _get_sync_db()
    q = f"%{query}%"
    rows = db.execute(
        "SELECT page_id, title, body, url, space_id, fetched_at FROM confluence_pages "
        "WHERE title LIKE ? OR body LIKE ? ORDER BY fetched_at DESC LIMIT ?",
        (q, q, limit),
    ).fetchall()
    return [{"page_id": r[0], "title": r[1], "body": r[2],
             "url": r[3], "space_id": r[4], "fetched_at": r[5]} for r in rows]


# -------------------------------------------------------------------
# High-level: fetch + cache in one call
# -------------------------------------------------------------------

def fetch_and_cache(page_id: str) -> dict | None:
    """Fetch page from Confluence API and store in cache. Returns page dict."""
    cached = get_cached_page(page_id)
    if cached:
        return cached
    page = fetch_page(page_id)
    if page:
        cache_page(page)
        logger.info("Cached Confluence page: %s — %s", page_id, page["title"])
    return page


def ingest_links_from_text(text: str) -> list[dict]:
    """Extract Confluence URLs from text, fetch and cache each page. Returns list of fetched pages."""
    ids = extract_page_ids(text)
    if not ids:
        return []
    pages = []
    for pid in ids:
        page = fetch_and_cache(pid)
        if page:
            pages.append(page)
    return pages
