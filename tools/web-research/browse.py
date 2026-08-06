#!/usr/bin/env python3
"""browse — Full interactive browser automation tool for The Goodman Protocol.

Provides a persistent stealth Chromium browser that Saul can control
step-by-step via CLI commands. The browser stays open between commands
so multi-step interactions work (booking forms, check-ins, dashboards).

Architecture:
  - Chromium runs as a background daemon with persistent profile
  - Commands are sent via Unix domain socket (fast, no port conflicts)
  - Anti-detection: stealth flags, real user-agent, persistent cookies
  - Auto-starts daemon on first command if not running

Usage:
  browse goto https://example.com
  browse describe                    # See page structure
  browse fill "#email" "me@mail.com"
  browse click "#submit"
  browse text ".results"
"""

import asyncio
import base64
import json
import os
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

PROJ_ROOT = Path(__file__).resolve().parent.parent.parent
DATA_DIR = PROJ_ROOT / "data" / "browser_profile"
CHROMIUM_DIR = DATA_DIR / "chromium_data"
SOCKET_PATH = DATA_DIR / ".browse.sock"
PID_FILE = DATA_DIR / ".browse.pid"
SCREENSHOT_DIR = DATA_DIR / "screenshots"
DOWNLOAD_DIR = DATA_DIR / "downloads"

# VNC configuration
VNC_DISPLAY = ":99"
VNC_PORT = 5900
NOVNC_PORT = 6080
NOVNC_WEB_DIR = "/usr/share/novnc"
VNC_FLAG_FILE = DATA_DIR / ".browse_vnc"      # presence = start in VNC mode
VNC_INFO_FILE = DATA_DIR / ".browse_vnc_info"  # JSON with port/URL/PIDs

for d in [DATA_DIR, CHROMIUM_DIR, SCREENSHOT_DIR, DOWNLOAD_DIR]:
    d.mkdir(parents=True, exist_ok=True)

USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

STEALTH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-sync",
    "--disable-background-networking",
    "--disable-component-update",
    "--disable-features=Translate,MediaRouter",
    "--disable-dev-shm-usage",
    "--no-sandbox",
    "--disable-setuid-sandbox",
    "--window-size=1280,900",
    "--start-maximized",
]

# JavaScript stealth patches — injected before any page script runs.
STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

if (!window.chrome) {
    window.chrome = {
        runtime: {
            onConnect: { addListener: function() {} },
            onMessage: { addListener: function() {} },
        },
        loadTimes: function() { return {} },
        csi: function() { return {} },
    };
}

const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);

Object.defineProperty(navigator, 'plugins', {
    get: () => {
        const plugins = [
            { name: 'Chrome PDF Plugin', filename: 'internal-pdf-viewer', description: 'Portable Document Format' },
            { name: 'Chrome PDF Viewer', filename: 'mhjfbmdgcfjbbpaeojofohoefgiehjai', description: '' },
            { name: 'Native Client', filename: 'internal-nacl-plugin', description: '' },
        ];
        plugins.length = 3;
        return plugins;
    }
});

Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';
    if (parameter === 37446) return 'Intel Iris OpenGL Engine';
    return getParameter.call(this, parameter);
};
"""

MAX_TEXT = 15000
MAX_PARALLEL_TABS = 8
PFETCH_TIMEOUT = 45


# ============================================================
# DAEMON — Persistent browser process, accepts commands
# ============================================================

def _start_vnc_services(novnc_port: int = NOVNC_PORT) -> list:
    """Start Xvfb, x11vnc, and websockify. Return list of Popen objects."""
    procs = []

    # 1. Start Xvfb
    xvfb = subprocess.Popen(
        ["Xvfb", VNC_DISPLAY, "-screen", "0", "1280x900x24", "-ac"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    procs.append(xvfb)
    time.sleep(1)  # Give Xvfb time to create the display

    # Set DISPLAY for this process and children (Chromium)
    os.environ["DISPLAY"] = VNC_DISPLAY

    # 2. Start x11vnc
    x11vnc = subprocess.Popen(
        [
            "x11vnc", "-display", VNC_DISPLAY,
            "-nopw", "-listen", "localhost",
            "-xkb", "-ncache", "10", "-ncache_cr",
            "-forever", "-quiet",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    procs.append(x11vnc)
    time.sleep(0.5)

    # 3. Start websockify (serves noVNC)
    websockify = subprocess.Popen(
        [
            "websockify", "--web", NOVNC_WEB_DIR,
            str(novnc_port), f"localhost:{VNC_PORT}",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    procs.append(websockify)
    time.sleep(0.5)

    # Write VNC info file
    vnc_info = {
        "novnc_port": novnc_port,
        "vnc_port": VNC_PORT,
        "display": VNC_DISPLAY,
        "novnc_url": f"http://localhost:{novnc_port}/vnc.html",
        "pids": [p.pid for p in procs],
    }
    VNC_INFO_FILE.write_text(json.dumps(vnc_info))

    return procs


def _stop_vnc_services():
    """Kill all VNC-related subprocesses and clean up files."""
    # Kill by info file PIDs
    if VNC_INFO_FILE.exists():
        try:
            info = json.loads(VNC_INFO_FILE.read_text())
            for pid in info.get("pids", []):
                try:
                    os.kill(pid, signal.SIGTERM)
                except OSError:
                    pass
        except (json.JSONDecodeError, KeyError):
            pass
        VNC_INFO_FILE.unlink(missing_ok=True)
    VNC_FLAG_FILE.unlink(missing_ok=True)


async def _daemon_main():
    """Launch browser and listen for commands on Unix socket."""
    from patchright.async_api import async_playwright

    # Check if VNC mode was requested
    vnc_mode = VNC_FLAG_FILE.exists()
    vnc_procs = []
    novnc_port = NOVNC_PORT

    if vnc_mode:
        # Read optional port override from flag file
        try:
            flag_data = VNC_FLAG_FILE.read_text().strip()
            if flag_data:
                novnc_port = int(flag_data)
        except (ValueError, OSError):
            pass
        print(f"Starting VNC services (display={VNC_DISPLAY}, noVNC port={novnc_port})...", flush=True)
        vnc_procs = _start_vnc_services(novnc_port)
        print(f"noVNC URL: http://localhost:{novnc_port}/vnc.html", flush=True)

    pw = await async_playwright().start()
    ctx = await pw.chromium.launch_persistent_context(
        user_data_dir=str(CHROMIUM_DIR),
        headless=not vnc_mode,
        args=STEALTH_ARGS,
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="Africa/Cairo",
        ignore_https_errors=True,
        accept_downloads=True,
        downloads_path=str(DOWNLOAD_DIR),
    )
    # Inject stealth JS via CDP — using add_init_script() breaks DNS resolution
    # in patchright's persistent context (chromium bug with protocol interception)
    _init_page = ctx.pages[0] if ctx.pages else await ctx.new_page()
    cdp = await ctx.new_cdp_session(_init_page)
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
    await cdp.detach()

    # --- Console & network capture ---
    _console_msgs: list[dict] = []
    _network_log: list[dict] = []
    _MAX_LOG = 200

    def _setup_page_listeners(page):
        """Attach console and network listeners to a page."""
        page.on("console", lambda msg: _console_msgs.append({
            "type": msg.type, "text": msg.text[:500],
            "url": page.url[:100], "ts": time.time(),
        }) if len(_console_msgs) < _MAX_LOG else None)

        page.on("pageerror", lambda err: _console_msgs.append({
            "type": "error", "text": str(err)[:500],
            "url": page.url[:100], "ts": time.time(),
        }) if len(_console_msgs) < _MAX_LOG else None)

        page.on("response", lambda resp: _network_log.append({
            "url": resp.url[:200], "status": resp.status,
            "method": resp.request.method,
            "type": resp.headers.get("content-type", "")[:60],
            "ts": time.time(),
        }) if len(_network_log) < _MAX_LOG else None)

    # Setup listeners on existing pages
    for p in ctx.pages:
        _setup_page_listeners(p)

    # Listen for new pages
    ctx.on("page", _setup_page_listeners)

    # --- Download tracking ---
    _downloads: list[dict] = []

    async def _handle_download(download):
        """Handle a download event — save to download dir."""
        try:
            suggested = download.suggested_filename
            path = DOWNLOAD_DIR / suggested
            # Avoid overwriting
            if path.exists():
                stem = path.stem
                suffix = path.suffix
                i = 1
                while path.exists():
                    path = DOWNLOAD_DIR / f"{stem}_{i}{suffix}"
                    i += 1
            await download.save_as(str(path))
            _downloads.append({
                "filename": suggested,
                "saved_as": str(path),
                "url": download.url,
                "ts": time.time(),
            })
        except Exception as e:
            _downloads.append({
                "filename": download.suggested_filename,
                "error": str(e),
                "url": download.url,
                "ts": time.time(),
            })

    # Set up download handling on the context
    ctx.on("page", lambda page: page.on("download", lambda dl: asyncio.ensure_future(_handle_download(dl))))
    for p in ctx.pages:
        p.on("download", lambda dl: asyncio.ensure_future(_handle_download(dl)))

    if not ctx.pages:
        await ctx.new_page()

    active_idx = 0
    _refs: dict[str, str] = {}  # "e1" -> "[data-br='e1']" (active page refs)
    _tab_refs: dict[int, dict[str, str]] = {}  # page_id -> {ref -> selector}

    def _page():
        nonlocal active_idx
        pages = ctx.pages
        if not pages:
            return None
        if active_idx >= len(pages):
            active_idx = len(pages) - 1
        return pages[active_idx]

    def _resolve_sel(sel: str, page=None) -> str:
        """Resolve eN refs to selectors, pass through CSS selectors as-is."""
        if page is not None:
            tab_r = _tab_refs.get(id(page), {})
            if sel in tab_r:
                return tab_r[sel]
        if sel in _refs:
            return _refs[sel]
        return sel

    async def _take_snapshot(page) -> str:
        """Capture all interactable elements, assign stable refs, return summary."""
        _refs.clear()
        _tab_refs[id(page)] = {}
        elements = await page.evaluate("""() => {
            // Remove old refs
            document.querySelectorAll('[data-br]').forEach(el => el.removeAttribute('data-br'));

            const results = [];
            let idx = 0;

            function visible(el) {
                const r = el.getBoundingClientRect();
                if (r.width === 0 && r.height === 0) return false;
                const s = getComputedStyle(el);
                return s.display !== 'none' && s.visibility !== 'hidden' && s.opacity !== '0';
            }

            function addEl(el, kind, extra) {
                if (!visible(el)) return;
                idx++;
                const ref = 'e' + idx;
                el.setAttribute('data-br', ref);
                const text = (el.innerText || el.value || el.placeholder || el.alt || el.title || '').trim().substring(0, 80);
                results.push({ref, kind, text, tag: el.tagName.toLowerCase(), ...extra});
            }

            // Links
            document.querySelectorAll('a[href]').forEach(el => {
                addEl(el, 'link', {href: el.href});
            });

            // Buttons
            document.querySelectorAll('button, [role="button"], input[type="submit"], input[type="button"]').forEach(el => {
                if (el.tagName === 'A') return; // already captured
                addEl(el, 'button', {});
            });

            // Form inputs
            document.querySelectorAll('input, select, textarea').forEach(el => {
                if (el.type === 'hidden' || el.type === 'submit' || el.type === 'button') return;
                const extra = {inputType: el.type || el.tagName.toLowerCase()};
                if (el.tagName === 'SELECT') {
                    const opts = [];
                    el.querySelectorAll('option').forEach(o => {
                        if (o.value) opts.push(o.text.trim().substring(0, 30));
                    });
                    extra.options = opts.slice(0, 8);
                }
                // Find label
                let label = '';
                if (el.id) {
                    const lb = document.querySelector('label[for="' + el.id + '"]');
                    if (lb) label = lb.innerText.trim().substring(0, 60);
                }
                if (!label) label = el.placeholder || el.name || el.id || '';
                extra.label = label;
                extra.value = el.type === 'password' ? '***' : (el.value || '').substring(0, 40);
                extra.required = el.required;
                addEl(el, 'input', extra);
            });

            return results;
        }""")

        # Build ref map (both global for active page and per-tab)
        page_refs = {}
        for el in elements:
            ref = el["ref"]
            selector = f'[data-br="{ref}"]'
            _refs[ref] = selector
            page_refs[ref] = selector
        _tab_refs[id(page)] = page_refs

        # Format output
        title = await page.title()
        lines = [f"SNAPSHOT: {title}", f"URL: {page.url}", f"({len(elements)} elements)", ""]

        for el in elements:
            ref = el["ref"]
            kind = el["kind"]

            if kind == "link":
                text = el.get("text", "")[:60]
                href = el.get("href", "")
                lines.append(f"  {ref}  [link] \"{text}\"  ->  {href}")

            elif kind == "button":
                text = el.get("text", "")[:60]
                lines.append(f"  {ref}  [button] \"{text}\"")

            elif kind == "input":
                itype = el.get("inputType", "text")
                label = el.get("label", "")
                val = el.get("value", "")
                req = " *" if el.get("required") else ""
                line = f"  {ref}  [{itype}] \"{label}\"{req}"
                if val:
                    line += f"  (value: {val})"
                opts = el.get("options")
                if opts:
                    line += f"  options: [{', '.join(opts)}]"
                lines.append(line)

        if not elements:
            lines.append("  (no interactable elements found)")

        return "\n".join(lines)

    async def _screenshot_compressed(page, full_page: bool = False,
                                     max_kb: int = 500) -> bytes:
        """Take JPEG screenshot with iterative quality reduction to stay under max_kb."""
        qualities = [75, 50, 30] if not full_page else [60, 40, 25]
        for q in qualities:
            img_bytes = await page.screenshot(
                full_page=full_page, type="jpeg", quality=q
            )
            if len(img_bytes) / 1024 <= max_kb:
                return img_bytes
        # Return whatever we got at lowest quality
        return img_bytes

    # ----------------------------------------------------------
    # Parallel browsing helpers
    # ----------------------------------------------------------
    async def _extract_page_text(page, max_chars: int = MAX_TEXT) -> str:
        """Extract readable text from a loaded page using Readability + html2text."""
        html = await page.content()
        title = await page.title()
        url = page.url

        try:
            from readability import Document
            doc = Document(html, url=url)
            article_html = doc.summary()
            doc_title = doc.short_title() or title
            try:
                import html2text
                h = html2text.HTML2Text()
                h.body_width = 0
                h.ignore_images = True
                h.skip_internal_links = True
                h.inline_links = True
                md = h.handle(article_html).strip()
                if len(md) > 100:
                    if doc_title and not md.startswith(f"# {doc_title}"):
                        md = f"# {doc_title}\n\n{md}"
                    return md[:max_chars]
            except Exception:
                pass
            text = _extract_text_from_html(article_html)
            if len(text) > 100:
                return f"{doc_title}\n\n{text}"[:max_chars]
        except Exception:
            pass

        text = await page.evaluate(
            "(max) => (document.querySelector('main, article, [role=\"main\"]') || document.body)"
            ".innerText.substring(0, max)",
            max_chars,
        )
        return f"{title}\n\n{text}" if text else f"{title}\n(empty page)"

    def _extract_text_from_html(html: str) -> str:
        """Regex fallback for text extraction."""
        import re
        text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
        text = re.sub(r'<[^>]+>', ' ', text)
        text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
        text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
        text = re.sub(r'\s+', ' ', text).strip()
        return text

    async def _solve_cloudflare(page) -> bool:
        """Attempt to solve Cloudflare challenge. Returns True if solved."""
        title = await page.title()
        if "just a moment" not in title.lower():
            body_len = len(await page.evaluate("() => document.body.innerText"))
            if body_len > 500:
                return True
        for frame in page.frames:
            if "challenges.cloudflare.com" in frame.url:
                try:
                    el = await frame.frame_element()
                    bb = await el.bounding_box() if el else None
                    if bb and bb["width"] > 0:
                        await page.mouse.click(bb["x"] + 25, bb["y"] + bb["height"] / 2)
                        await asyncio.sleep(2)
                except Exception:
                    pass
                break
        for _ in range(8):
            await asyncio.sleep(2)
            title = await page.title()
            body = await page.evaluate("() => document.body.innerText")
            if "just a moment" not in title.lower() and len(body) > 500:
                return True
        return False

    async def _fetch_one(ctx, url: str, max_chars: int,
                         setup_listeners) -> dict:
        """Open a new tab, navigate, extract text, close tab. Returns result dict."""
        page = await ctx.new_page()
        setup_listeners(page)
        try:
            resp = await page.goto(url, timeout=PFETCH_TIMEOUT * 1000,
                                   wait_until="domcontentloaded")
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                await asyncio.sleep(2)
            status = resp.status if resp else "?"
            title = await page.title()
            if "just a moment" in title.lower() or status == 403:
                await _solve_cloudflare(page)
            text = await _extract_page_text(page, max_chars)
            final_url = page.url
            final_title = await page.title()
            return _ok(f"[{status}] {final_title}\n{final_url}\n\n{text}")
        except asyncio.CancelledError:
            return _err(f"fetch {url}: cancelled")
        except Exception as e:
            return _err(f"fetch {url}: {type(e).__name__}: {e}")
        finally:
            try:
                if not page.is_closed():
                    await page.close()
            except Exception:
                pass

    async def _pfetch(ctx, urls: list[str], max_chars: int,
                      setup_listeners) -> str:
        """Fetch multiple URLs in parallel tabs. Returns combined text."""
        pages_before = set(id(p) for p in ctx.pages)
        sem = asyncio.Semaphore(MAX_PARALLEL_TABS)

        async def _one(url: str) -> dict:
            async with sem:
                try:
                    return await asyncio.wait_for(
                        _fetch_one(ctx, url, max_chars, setup_listeners),
                        timeout=PFETCH_TIMEOUT + 25,
                    )
                except asyncio.TimeoutError:
                    return _err(f"fetch {url}: timed out after {PFETCH_TIMEOUT + 25}s")

        tasks = [asyncio.ensure_future(_one(u)) for u in urls]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        # Close any orphaned tabs from timed-out fetches
        for p in list(ctx.pages):
            if id(p) not in pages_before and not p.is_closed():
                try:
                    await p.close()
                except Exception:
                    pass

        parts = []
        for i, r in enumerate(results):
            url = urls[i]
            parts.append(f"{'='*60}")
            parts.append(f"[{i+1}/{len(urls)}] {url}")
            parts.append(f"{'='*60}")
            if isinstance(r, Exception):
                parts.append(f"ERROR: {r}")
            elif r.get("ok"):
                parts.append(r["result"])
            else:
                parts.append(f"ERROR: {r.get('error', '?')}")
            parts.append("")
        return "\n".join(parts)

    async def _exec_on_page(cmd: dict, page) -> dict:
        """Execute a command targeting a specific page (for batch mode)."""
        action = cmd.get("action", "")
        a = cmd.get("args", [])
        try:
            if action == "goto":
                url = a[0] if a else ""
                if not url.startswith(("http://", "https://", "file://")):
                    url = "https://" + url
                resp = await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    await asyncio.sleep(2)
                status = resp.status if resp else "?"
                return _ok(f"[{status}] {await page.title()}\n{page.url}")
            if action == "text":
                sel = _resolve_sel(a[0] if a else "body", page)
                el = await page.query_selector(sel)
                if not el:
                    return _err(f"Not found: {a[0] if a else 'body'}")
                return _ok(_trunc(await el.inner_text()))
            if action == "snapshot":
                return _ok(await _take_snapshot(page))
            if action == "click":
                sel = a[0] if a else ""
                sel = _resolve_sel(sel, page)
                if sel and not any(c in sel for c in "#.[]:>+~="):
                    sel = f'text="{sel}"'
                await page.click(sel, timeout=10000)
                await asyncio.sleep(1)
                return _ok(f"Clicked: {a[0] if a else sel}")
            if action == "extract":
                max_chars = int(a[0]) if a else MAX_TEXT
                text = await _extract_page_text(page, max_chars)
                return _ok(text)
            if action == "eval":
                js = a[0] if a else ""
                result = await page.evaluate(js)
                s = json.dumps(result, indent=2, default=str) if result is not None else "(void)"
                return _ok(_trunc(s))
            return _err(f"batch doesn't support: {action} (use goto/text/snapshot/click/extract/eval)")
        except Exception as e:
            return _err(f"{type(e).__name__}: {e}")

    # ----------------------------------------------------------
    # Command dispatcher
    # ----------------------------------------------------------
    async def _exec(cmd: dict) -> dict:
        nonlocal active_idx
        action = cmd.get("action", "")
        a = cmd.get("args", [])
        page = _page()

        try:
            # --- Navigation ---
            if action == "goto":
                url = a[0] if a else ""
                if not url.startswith(("http://", "https://", "file://")):
                    url = "https://" + url
                resp = await page.goto(url, timeout=30000, wait_until="domcontentloaded")
                try:
                    await page.wait_for_load_state("networkidle", timeout=10000)
                except Exception:
                    await asyncio.sleep(2)
                status = resp.status if resp else "?"
                # Check for Cloudflare challenge — try to solve it
                title = await page.title()
                if "just a moment" in title.lower() or status == 403:
                    body = await page.evaluate("() => document.body.innerText")
                    if len(body) < 500 and any(s in body.lower() for s in [
                        "just a moment", "checking your browser",
                        "verify you are human", "security verification",
                    ]):
                        # Click Turnstile checkbox if present
                        for frame in page.frames:
                            if "challenges.cloudflare.com" in frame.url:
                                try:
                                    el = await frame.frame_element()
                                    bb = await el.bounding_box() if el else None
                                    if bb and bb["width"] > 0:
                                        await page.mouse.click(
                                            bb["x"] + 25, bb["y"] + bb["height"] / 2
                                        )
                                        await asyncio.sleep(2)
                                except Exception:
                                    pass
                                break
                        for _ in range(8):
                            await asyncio.sleep(2)
                            title = await page.title()
                            body = await page.evaluate("() => document.body.innerText")
                            if "just a moment" not in title.lower() and len(body) > 500:
                                break
                return _ok(f"[{status}] {await page.title()}\n{page.url}")

            if action == "back":
                await page.go_back(timeout=10000)
                return _ok(f"Back → {await page.title()} — {page.url}")

            if action == "forward":
                await page.go_forward(timeout=10000)
                return _ok(f"Forward → {await page.title()} — {page.url}")

            if action == "reload":
                await page.reload(timeout=30000)
                return _ok(f"Reloaded: {await page.title()}")

            # --- Snapshot ---
            if action == "snapshot":
                return _ok(await _take_snapshot(page))

            # --- ARIA accessibility tree via CDP ---
            if action == "aria":
                cdp = await page.context.new_cdp_session(page)
                try:
                    tree = await cdp.send("Accessibility.getFullAXTree")
                finally:
                    await cdp.detach()

                nodes = tree.get("nodes", [])
                if not nodes:
                    return _ok("No accessibility tree available.")

                # Build lookup: nodeId -> node
                node_map = {}
                for n in nodes:
                    node_map[n["nodeId"]] = n

                # Roles to skip display (structural noise) — still recurse children
                SKIP_ROLES = {
                    "generic", "none", "presentation", "InlineTextBox",
                    "LineBreak", "StaticText", "LabelText",
                }

                # Extract role, name, value, props from a node
                def _node_info(n):
                    role = n.get("role", {}).get("value", "")
                    name = n.get("name", {}).get("value", "")
                    val_obj = n.get("value", {})
                    value = val_obj.get("value", "") if val_obj else ""

                    props = {}
                    for p in n.get("properties", []):
                        pname = p.get("name", "")
                        pval = p.get("value", {})
                        props[pname] = pval.get("value", "") if isinstance(pval, dict) else pval

                    return role, name, value, props

                def _gather_text(n):
                    """Collect text from StaticText children for nodes with no name."""
                    texts = []
                    for cid in n.get("childIds", []):
                        child = node_map.get(cid)
                        if not child:
                            continue
                        cr = child.get("role", {}).get("value", "")
                        if cr == "StaticText":
                            t = child.get("name", {}).get("value", "")
                            if t:
                                texts.append(t)
                    return " ".join(texts)[:120]

                # Build tree lines with indentation
                MAX_NODES = 2000
                output_count = 0
                lines = []

                def _format_node(node_id, depth=0):
                    nonlocal output_count
                    if output_count >= MAX_NODES:
                        return
                    n = node_map.get(node_id)
                    if not n:
                        return

                    role, name, value, props = _node_info(n)
                    is_ignored = n.get("ignored", False)

                    # Decide whether to display this node
                    skip_self = is_ignored or role in SKIP_ROLES

                    if not skip_self:
                        indent = "  " * depth
                        parts = [f"[{role}"]

                        # Add level for headings
                        level = props.get("level")
                        if level:
                            parts[0] += f" level={level}"

                        parts[0] += "]"

                        # Add accessible name; fall back to StaticText children
                        if not name:
                            name = _gather_text(n)
                        if name:
                            parts.append(f'"{name[:120]}"')

                        # Add value for inputs
                        if value and value != name:
                            parts.append(f'value="{value[:80]}"')

                        # States
                        states = []
                        if props.get("focused"):
                            states.append("focused")
                        checked = props.get("checked", "")
                        if checked == "true" or checked is True:
                            states.append("checked")
                        elif checked == "mixed":
                            states.append("mixed")
                        if props.get("disabled"):
                            states.append("disabled")
                        expanded = props.get("expanded", "")
                        if expanded is True or expanded == "true":
                            states.append("expanded")
                        elif expanded is False or expanded == "false":
                            states.append("collapsed")
                        if props.get("required"):
                            states.append("required")
                        if props.get("selected"):
                            states.append("selected")
                        if props.get("readonly"):
                            states.append("readonly")

                        if states:
                            parts.append(f"({', '.join(states)})")

                        # Interactive element extras
                        if role in ("textbox", "searchbox", "spinbutton"):
                            itype = props.get("inputType", "")
                            if itype:
                                parts.append(f"type={itype}")
                        if role == "link":
                            url = props.get("url", "")
                            if url:
                                parts.append(f"-> {url[:120]}")

                        lines.append(indent + " ".join(parts))
                        output_count += 1

                    # Always recurse children (even for ignored/skipped nodes)
                    child_ids = n.get("childIds", [])
                    child_depth = depth if skip_self else depth + 1
                    for cid in child_ids:
                        if output_count >= MAX_NODES:
                            break
                        _format_node(cid, child_depth)

                # Find root node(s) — nodes with no parentId or parentId not in map
                roots = []
                for n in nodes:
                    pid = n.get("parentId")
                    if pid is None or pid not in node_map:
                        roots.append(n["nodeId"])

                if not roots and nodes:
                    roots = [nodes[0]["nodeId"]]

                for rid in roots:
                    _format_node(rid)

                title = await page.title()
                total = len(nodes)
                header = [
                    f"ARIA TREE: {title}",
                    f"URL: {page.url}",
                    f"({output_count} of {total} nodes shown)",
                    "",
                ]
                if output_count >= MAX_NODES:
                    header.append(f"[truncated at {MAX_NODES} nodes]")
                    header.append("")

                return _ok("\n".join(header + lines))

            # --- Interaction (all support eN refs) ---
            if action == "click":
                sel = a[0] if a else ""
                sel = _resolve_sel(sel)
                # Support text-based selectors: click "Submit" → text=Submit
                if sel and not any(c in sel for c in "#.[]:>+~="):
                    sel = f'text="{sel}"'
                await page.click(sel, timeout=10000)
                await asyncio.sleep(1)
                return _ok(f"Clicked: {a[0] if a else sel}\nNow on: {await page.title()} — {page.url}")

            if action == "fill":
                sel = _resolve_sel(a[0]) if len(a) > 0 else ""
                val = a[1] if len(a) > 1 else ""
                await page.fill(sel, val, timeout=10000)
                return _ok(f"Filled {a[0]} = \"{val}\"")

            if action == "type":
                sel = _resolve_sel(a[0]) if len(a) > 0 else ""
                val = a[1] if len(a) > 1 else ""
                await page.type(sel, val, delay=50)
                return _ok(f"Typed into {a[0]}")

            if action == "press":
                key = a[0] if a else "Enter"
                await page.keyboard.press(key)
                await asyncio.sleep(0.5)
                return _ok(f"Pressed {key}")

            if action == "keytype":
                # Type into the CURRENTLY FOCUSED element via the keyboard, which crosses
                # iframe boundaries (unlike page.type, which needs a main-frame selector).
                # Handles arbitrary unicode + newlines (Enter). Focus the target first (click).
                val = a[0] if a else ""
                await page.keyboard.type(val, delay=6)
                return _ok(f"Keytyped {len(val)} chars into focused element")

            if action == "filearm":
                # Arm a one-shot file-chooser handler: the NEXT file chooser opened by ANY
                # click (including a hidden <input type=file> inside an iframe) gets these
                # files set. Frame-agnostic — the file chooser is a page-level event.
                paths = [p.strip() for p in (a[0] if a else "").split("|") if p.strip()]

                def _on_fc(fc, _p=paths):
                    asyncio.create_task(
                        fc.set_files(_p if len(_p) > 1 else _p[0]))
                page.once("filechooser", _on_fc)
                return _ok(f"Armed filechooser -> {paths}")

            if action == "select":
                sel = _resolve_sel(a[0]) if len(a) > 0 else ""
                val = a[1] if len(a) > 1 else ""
                await page.select_option(sel, val, timeout=10000)
                return _ok(f"Selected \"{val}\" in {a[0]}")

            if action == "check":
                sel = _resolve_sel(a[0]) if a else ""
                await page.check(sel, timeout=10000)
                return _ok(f"Checked {a[0]}")

            if action == "uncheck":
                sel = _resolve_sel(a[0]) if a else ""
                await page.uncheck(sel, timeout=10000)
                return _ok(f"Unchecked {a[0]}")

            if action == "hover":
                sel = _resolve_sel(a[0]) if a else ""
                await page.hover(sel, timeout=10000)
                return _ok(f"Hovering {a[0]}")

            # --- Page content ---
            if action == "text":
                sel = _resolve_sel(a[0]) if a else "body"
                el = await page.query_selector(sel)
                if not el:
                    return _err(f"Not found: {a[0] if a else 'body'}")
                txt = await el.inner_text()
                return _ok(_trunc(txt))

            if action == "html":
                sel = _resolve_sel(a[0]) if a else "body"
                el = await page.query_selector(sel)
                if not el:
                    return _err(f"Not found: {a[0] if a else 'body'}")
                h = await el.inner_html()
                return _ok(_trunc(h))

            if action == "title":
                return _ok(await page.title())

            if action == "url":
                return _ok(page.url)

            if action == "screenshot":
                name = a[0] if a else f"shot_{int(time.time())}"
                if not name.endswith(".png"):
                    name += ".png"
                p = SCREENSHOT_DIR / name
                await page.screenshot(path=str(p), full_page=False)
                return _ok(f"Screenshot saved: {p}")

            if action == "fullshot":
                name = a[0] if a else f"full_{int(time.time())}"
                if not name.endswith(".png"):
                    name += ".png"
                p = SCREENSHOT_DIR / name
                await page.screenshot(path=str(p), full_page=True)
                return _ok(f"Full-page screenshot saved: {p}")

            if action == "look":
                img_bytes = await _screenshot_compressed(page, full_page=False)
                b64 = base64.b64encode(img_bytes).decode()
                size_kb = len(img_bytes) / 1024
                title = await page.title()
                return _ok(
                    f"VISUAL_SNAPSHOT: {title}\n"
                    f"URL: {page.url}\n"
                    f"Image: {size_kb:.0f}KB JPEG\n"
                    f"[screenshot:data:image/jpeg;base64,{b64}]"
                )

            if action == "lookfull":
                img_bytes = await _screenshot_compressed(page, full_page=True)
                b64 = base64.b64encode(img_bytes).decode()
                size_kb = len(img_bytes) / 1024
                title = await page.title()
                return _ok(
                    f"VISUAL_SNAPSHOT (full page): {title}\n"
                    f"URL: {page.url}\n"
                    f"Image: {size_kb:.0f}KB JPEG\n"
                    f"[screenshot:data:image/jpeg;base64,{b64}]"
                )

            # --- Describe: rich page analysis ---
            if action == "describe":
                info = await page.evaluate("""() => {
                    const r = {title: document.title, url: location.href,
                               forms: [], buttons: [], links: [], text: ''};

                    // Form fields
                    document.querySelectorAll('input, select, textarea').forEach(el => {
                        if (el.type === 'hidden') return;
                        const f = {
                            tag: el.tagName.toLowerCase(),
                            type: el.type || '',
                            name: el.name || '',
                            id: el.id || '',
                            placeholder: el.placeholder || '',
                            value: el.type === 'password' ? '***' : (el.value || ''),
                            required: el.required,
                            label: '', selector: ''
                        };
                        if (el.id) {
                            const lb = document.querySelector('label[for="' + el.id + '"]');
                            if (lb) f.label = lb.innerText.trim().substring(0, 80);
                            f.selector = '#' + el.id;
                        } else if (el.name) {
                            f.selector = '[name="' + el.name + '"]';
                        } else if (el.placeholder) {
                            f.selector = '[placeholder="' + el.placeholder + '"]';
                        }
                        if (el.tagName === 'SELECT') {
                            f.options = [];
                            el.querySelectorAll('option').forEach(o => {
                                if (o.value) f.options.push({v: o.value, t: o.innerText.trim()});
                            });
                            f.options = f.options.slice(0, 15);
                        }
                        r.forms.push(f);
                    });

                    // Buttons
                    document.querySelectorAll(
                        'button, [role="button"], input[type="submit"], input[type="button"], a.btn, a.button'
                    ).forEach(b => {
                        const text = (b.innerText || b.value || '').trim().substring(0, 60);
                        if (!text) return;
                        let sel = '';
                        if (b.id) sel = '#' + b.id;
                        else if (b.className && typeof b.className === 'string') {
                            const cls = b.className.split(' ').filter(c => c).slice(0, 2).join('.');
                            if (cls) sel = b.tagName.toLowerCase() + '.' + cls;
                        }
                        r.buttons.push({text, sel, type: b.type || '', tag: b.tagName.toLowerCase()});
                    });

                    // Links
                    document.querySelectorAll('a[href]').forEach(a => {
                        const text = a.innerText.trim().substring(0, 80);
                        if (text && text.length > 1)
                            r.links.push({text, href: a.href});
                    });
                    r.links = r.links.slice(0, 40);

                    // Text preview
                    const main = document.querySelector(
                        'main, article, [role="main"], .content, #content'
                    );
                    r.text = (main || document.body).innerText.substring(0, 3000);

                    return r;
                }""")

                lines = [f"PAGE: {info['title']}", f"URL: {info['url']}", ""]

                if info.get("forms"):
                    lines.append("FORM FIELDS:")
                    for f in info["forms"]:
                        label = (f.get("label") or f.get("placeholder")
                                 or f.get("name") or f.get("id") or "(unnamed)")
                        sel = f.get("selector", "")
                        ftype = f.get("type") or f.get("tag", "")
                        val = f.get("value", "")
                        req = " *REQUIRED*" if f.get("required") else ""
                        ln = f"  [{ftype}] {label}{req}"
                        if sel:
                            ln += f"  ->  {sel}"
                        if val:
                            ln += f"  (value: {val})"
                        if f.get("options"):
                            opts = ", ".join(o["t"] for o in f["options"][:8])
                            ln += f"\n        options: [{opts}]"
                        lines.append(ln)
                    lines.append("")

                if info.get("buttons"):
                    lines.append("BUTTONS:")
                    for b in info["buttons"]:
                        sel_hint = f'  ->  {b["sel"]}' if b.get("sel") else ""
                        lines.append(f'  [{b["text"]}]{sel_hint}')
                    lines.append("")

                if info.get("links"):
                    lines.append(f"LINKS ({len(info['links'])}):")
                    for l in info["links"][:25]:
                        lines.append(f"  {l['text'][:60]}  ->  {l['href']}")
                    if len(info["links"]) > 25:
                        lines.append(f"  ... and {len(info['links']) - 25} more")
                    lines.append("")

                if info.get("text"):
                    preview = info["text"]
                    if len(preview) > 2000:
                        preview = preview[:2000] + "\n[... truncated ...]"
                    lines.append("VISIBLE TEXT:")
                    lines.append(preview)

                return _ok("\n".join(lines))

            # --- Find elements matching text ---
            if action == "find":
                query = a[0] if a else ""
                results = await page.evaluate("""(q) => {
                    const found = [];
                    const lower = q.toLowerCase();
                    const all = document.querySelectorAll('*');
                    for (const el of all) {
                        const text = (el.innerText || '').trim();
                        if (text && text.toLowerCase().includes(lower) && text.length < 200) {
                            let sel = '';
                            if (el.id) sel = '#' + el.id;
                            else if (el.className && typeof el.className === 'string') {
                                const c = el.className.split(' ').filter(x=>x).slice(0,2).join('.');
                                if (c) sel = el.tagName.toLowerCase() + '.' + c;
                            }
                            found.push({
                                tag: el.tagName.toLowerCase(),
                                text: text.substring(0, 120),
                                sel: sel,
                                clickable: ['A','BUTTON','INPUT'].includes(el.tagName) ||
                                           el.getAttribute('role') === 'button'
                            });
                        }
                        if (found.length >= 20) break;
                    }
                    return found;
                }""", query)
                if not results:
                    return _ok(f"Nothing found matching: {query}")
                lines = [f"Found {len(results)} matches for \"{query}\":"]
                for r in results:
                    click = " [clickable]" if r.get("clickable") else ""
                    sel = f"  ->  {r['sel']}" if r.get("sel") else ""
                    lines.append(f"  <{r['tag']}> {r['text']}{click}{sel}")
                return _ok("\n".join(lines))

            # --- Wait ---
            if action == "wait":
                sel = _resolve_sel(a[0]) if a else ""
                timeout_s = int(a[1]) if len(a) > 1 else 10
                await page.wait_for_selector(sel, timeout=timeout_s * 1000)
                return _ok(f"Found: {a[0] if a else sel}")

            if action == "sleep":
                secs = float(a[0]) if a else 2
                await asyncio.sleep(secs)
                return _ok(f"Waited {secs}s")

            # --- Scroll ---
            if action == "scroll":
                direction = a[0] if a else "down"
                px = int(a[1]) if len(a) > 1 else 500
                if direction == "up":
                    px = -px
                await page.evaluate(f"window.scrollBy(0, {px})")
                scroll_pos = await page.evaluate("window.scrollY")
                return _ok(f"Scrolled {direction} — position: {int(scroll_pos)}px")

            # --- JavaScript ---
            if action == "eval":
                js = a[0] if a else ""
                result = await page.evaluate(js)
                s = json.dumps(result, indent=2, default=str) if result is not None else "(void)"
                return _ok(_trunc(s))

            # --- Page management ---
            if action == "pages":
                out = []
                for i, p in enumerate(ctx.pages):
                    marker = " >>>" if i == active_idx else "    "
                    t = await p.title() or "(untitled)"
                    out.append(f"{marker} [{i}] {t} — {p.url}")
                return _ok("\n".join(out) or "No pages")

            if action == "switch":
                idx = int(a[0]) if a else 0
                if 0 <= idx < len(ctx.pages):
                    active_idx = idx
                    p = ctx.pages[idx]
                    return _ok(f"Switched to [{idx}]: {await p.title()} — {p.url}")
                return _err(f"Invalid index {idx}, have {len(ctx.pages)} pages")

            if action == "new":
                np = await ctx.new_page()
                active_idx = len(ctx.pages) - 1
                if a:
                    url = a[0]
                    if not url.startswith(("http://", "https://", "file://")):
                        url = "https://" + url
                    await np.goto(url, timeout=30000, wait_until="domcontentloaded")
                    try:
                        await np.wait_for_load_state("networkidle", timeout=8000)
                    except Exception:
                        pass
                return _ok(f"New page [{active_idx}]: {await np.title()} — {np.url}")

            if action == "close":
                if len(ctx.pages) <= 1:
                    return _err("Can't close the last page")
                await page.close()
                active_idx = max(0, active_idx - 1)
                p = ctx.pages[active_idx]
                return _ok(f"Closed. Now on [{active_idx}]: {await p.title()}")

            # --- Links / cookies ---
            if action == "links":
                sel = a[0] if a else "body"
                links = await page.evaluate("""(sel) => {
                    const c = document.querySelector(sel) || document.body;
                    const out = [];
                    c.querySelectorAll('a[href]').forEach(a => {
                        const t = a.innerText.trim();
                        if (t) out.push(t.substring(0,80) + '  ->  ' + a.href);
                    });
                    return out.slice(0, 50);
                }""", sel)
                return _ok("\n".join(links) if links else "No links found")

            if action == "cookies":
                domain = a[0] if a else None
                cookies = await ctx.cookies()
                if domain:
                    cookies = [c for c in cookies if domain in c.get("domain", "")]
                out = [f"{c['name']}={c['value'][:60]}  ({c['domain']})"
                       for c in cookies[:30]]
                return _ok("\n".join(out) if out else "No cookies")

            if action == "setcookie":
                # setcookie name value domain
                if len(a) < 3:
                    return _err("Usage: setcookie <name> <value> <domain>")
                await ctx.add_cookies([{
                    "name": a[0], "value": a[1], "domain": a[2],
                    "path": "/",
                }])
                return _ok(f"Cookie set: {a[0]}={a[1]} on {a[2]}")

            if action == "exportcookies":
                cookies = await ctx.cookies()
                data = json.dumps(cookies, indent=2, default=str)
                if a:
                    fpath = Path(a[0])
                    fpath.write_text(data, encoding="utf-8")
                    return _ok(f"Exported {len(cookies)} cookies to {fpath}")
                return _ok(data)

            if action == "importcookies":
                if not a:
                    return _err("Usage: importcookies <file>")
                fpath = Path(a[0])
                if not fpath.exists():
                    return _err(f"File not found: {fpath}")
                raw = fpath.read_text(encoding="utf-8").strip()
                # Detect JSON vs Netscape/curl tab-separated format
                cookies_list = []
                if raw.startswith("[") or raw.startswith("{"):
                    cookies_list = json.loads(raw)
                    if isinstance(cookies_list, dict):
                        cookies_list = [cookies_list]
                else:
                    # Netscape/curl tab-separated cookie format
                    for line in raw.splitlines():
                        line = line.strip()
                        if not line or line.startswith("#"):
                            continue
                        parts = line.split("\t")
                        if len(parts) >= 7:
                            cookies_list.append({
                                "domain": parts[0],
                                "path": parts[2],
                                "secure": parts[3].upper() == "TRUE",
                                "expires": int(float(parts[4])) if parts[4] not in ("0", "-1") else -1,
                                "name": parts[5],
                                "value": parts[6],
                            })
                # Ensure each cookie has at minimum name, value, domain
                valid = []
                for c in cookies_list:
                    if c.get("name") and "domain" in c:
                        if "value" not in c:
                            c["value"] = ""
                        valid.append(c)
                if not valid:
                    return _err("No valid cookies found in file")
                await ctx.add_cookies(valid)
                return _ok(f"Imported {len(valid)} cookies")

            if action == "clearcookies":
                domain = a[0] if a else None
                if domain:
                    before = await ctx.cookies()
                    matching = [c for c in before if domain in c.get("domain", "")]
                    count = len(matching)
                    await ctx.clear_cookies()
                    # Re-add non-matching cookies
                    keep = [c for c in before if domain not in c.get("domain", "")]
                    if keep:
                        await ctx.add_cookies(keep)
                    return _ok(f"Cleared {count} cookies matching domain '{domain}'")
                else:
                    before = await ctx.cookies()
                    count = len(before)
                    await ctx.clear_cookies()
                    return _ok(f"Cleared all {count} cookies")

            # --- localStorage / sessionStorage ---
            if action in ("localstorage", "sessionstorage"):
                storage_name = "localStorage" if action == "localstorage" else "sessionStorage"
                sub = a[0] if a else "list"

                if sub == "list":
                    data = await page.evaluate(
                        "(sn) => { const d = {}; const s = window[sn]; "
                        "for (let i = 0; i < s.length; i++) { const k = s.key(i); d[k] = s.getItem(k); } "
                        "return d; }",
                        storage_name,
                    )
                    if not data:
                        return _ok(f"{storage_name} is empty")
                    return _ok(json.dumps(data, indent=2))

                elif sub == "get":
                    key = a[1] if len(a) > 1 else ""
                    if not key:
                        return _err(f"Usage: {action} get <key>")
                    val = await page.evaluate(
                        "(args) => window[args[0]].getItem(args[1])",
                        [storage_name, key],
                    )
                    if val is not None:
                        return _ok(f"{key} = {val}")
                    return _ok(f"{key}: not found")

                elif sub == "set":
                    key = a[1] if len(a) > 1 else ""
                    val = a[2] if len(a) > 2 else ""
                    if not key:
                        return _err(f"Usage: {action} set <key> <value>")
                    await page.evaluate(
                        "(args) => window[args[0]].setItem(args[1], args[2])",
                        [storage_name, key, val],
                    )
                    return _ok(f"Set {storage_name}.{key} = {val}")

                elif sub == "delete":
                    key = a[1] if len(a) > 1 else ""
                    if not key:
                        return _err(f"Usage: {action} delete <key>")
                    await page.evaluate(
                        "(args) => window[args[0]].removeItem(args[1])",
                        [storage_name, key],
                    )
                    return _ok(f"Deleted {storage_name}.{key}")

                elif sub == "clear":
                    count = await page.evaluate(
                        "(sn) => { const c = window[sn].length; window[sn].clear(); return c; }",
                        storage_name,
                    )
                    return _ok(f"Cleared {count} items from {storage_name}")

                else:
                    return _err(f"Unknown {action} sub-command: {sub}. Use: list, get, set, delete, clear")

            # --- Console & network ---
            if action == "console":
                count = int(a[0]) if a else 20
                msgs = _console_msgs[-count:]
                if not msgs:
                    return _ok("No console messages captured.")
                lines = [f"Console ({len(msgs)} of {len(_console_msgs)} messages):"]
                for m in msgs:
                    lines.append(f"  [{m['type']}] {m['text']}")
                return _ok("\n".join(lines))

            if action == "network":
                count = int(a[0]) if a else 20
                filt = a[1] if len(a) > 1 else None
                entries = _network_log[-_MAX_LOG:]
                if filt:
                    entries = [e for e in entries if filt in e["url"]]
                entries = entries[-count:]
                if not entries:
                    return _ok("No network requests captured.")
                lines = [f"Network ({len(entries)} requests):"]
                for e in entries:
                    lines.append(f"  {e['method']} {e['status']} {e['url']}")
                return _ok("\n".join(lines))

            if action == "errors":
                errs = [m for m in _console_msgs if m["type"] == "error"]
                count = int(a[0]) if a else 20
                errs = errs[-count:]
                if not errs:
                    return _ok("No errors captured.")
                lines = [f"Errors ({len(errs)}):"]
                for e in errs:
                    lines.append(f"  {e['text']}")
                return _ok("\n".join(lines))

            # --- File downloads & uploads ---
            if action == "downloads":
                if not _downloads:
                    return _ok("No downloads captured yet.")
                lines = [f"Downloads ({len(_downloads)}):"]
                for i, d in enumerate(_downloads):
                    status = d.get("saved_as", f"ERROR: {d.get('error', '?')}")
                    lines.append(f"  [{i}] {d['filename']}  ->  {status}")
                return _ok("\n".join(lines))

            if action == "download":
                idx = int(a[0]) if a else -1
                if idx < 0 or idx >= len(_downloads):
                    return _err(f"Invalid index {idx}. Have {len(_downloads)} downloads.")
                d = _downloads[idx]
                info_lines = [
                    f"Download [{idx}]:",
                    f"  Filename: {d['filename']}",
                    f"  URL: {d['url']}",
                    f"  Time: {datetime.fromtimestamp(d['ts']).strftime('%Y-%m-%d %H:%M:%S')}",
                ]
                if "saved_as" in d:
                    info_lines.append(f"  Saved to: {d['saved_as']}")
                if "error" in d:
                    info_lines.append(f"  Error: {d['error']}")
                return _ok("\n".join(info_lines))

            if action == "upload":
                sel = _resolve_sel(a[0]) if len(a) > 0 else ""
                filepath = a[1] if len(a) > 1 else ""
                if not filepath:
                    return _err("Usage: upload <selector|eN> <filepath>")
                # Handle multiple files (comma-separated)
                files = [f.strip() for f in filepath.split(",")]
                for f in files:
                    if not Path(f).exists():
                        return _err(f"File not found: {f}")
                el = await page.query_selector(sel)
                if not el:
                    return _err(f"Element not found: {a[0]}")
                await el.set_input_files(files if len(files) > 1 else files[0])
                return _ok(f"Uploaded {len(files)} file(s) to {a[0]}")

            if action == "waitdownload":
                timeout_s = int(a[0]) if a else 30
                count_before = len(_downloads)
                for _ in range(timeout_s * 2):
                    await asyncio.sleep(0.5)
                    if len(_downloads) > count_before:
                        d = _downloads[-1]
                        return _ok(f"Download complete: {d['filename']} -> {d.get('saved_as', 'error')}")
                return _err(f"No download after {timeout_s}s")

            # --- Parallel browsing ---
            if action == "fetch":
                url = a[0] if a else ""
                if not url:
                    return _err("Usage: fetch <url>")
                if not url.startswith(("http://", "https://")):
                    url = "https://" + url
                max_chars = int(a[1]) if len(a) > 1 else MAX_TEXT
                result = await _fetch_one(ctx, url, max_chars, _setup_page_listeners)
                return result

            if action == "pfetch":
                if not a:
                    return _err("Usage: pfetch <url1> <url2> ... [--max-chars N]")
                max_chars = MAX_TEXT
                urls = []
                i = 0
                while i < len(a):
                    if a[i] == "--max-chars" and i + 1 < len(a):
                        max_chars = int(a[i + 1])
                        i += 2
                    else:
                        u = a[i]
                        if not u.startswith(("http://", "https://")):
                            u = "https://" + u
                        urls.append(u)
                        i += 1
                if not urls:
                    return _err("No URLs provided")
                if len(urls) > MAX_PARALLEL_TABS:
                    return _err(f"Max {MAX_PARALLEL_TABS} parallel URLs")
                results = await _pfetch(ctx, urls, max_chars, _setup_page_listeners)
                return _ok(results)

            if action == "batch":
                cmds = cmd.get("cmds", [])
                if not cmds:
                    return _err("batch requires 'cmds' list of {action, args, tab?} objects")
                if len(cmds) > MAX_PARALLEL_TABS:
                    return _err(f"Max {MAX_PARALLEL_TABS} parallel commands")
                pages = ctx.pages
                tasks = []
                valid = []
                results_map = {}
                for i, sub_cmd in enumerate(cmds):
                    tab_idx = sub_cmd.get("tab", active_idx)
                    if tab_idx < 0 or tab_idx >= len(pages):
                        results_map[i] = _err(f"Invalid tab {tab_idx}")
                    else:
                        sub = {"action": sub_cmd["action"], "args": sub_cmd.get("args", [])}
                        tasks.append(asyncio.ensure_future(_exec_on_page(sub, pages[tab_idx])))
                        valid.append(i)
                batch_results = await asyncio.gather(*tasks, return_exceptions=True)
                for j, idx in enumerate(valid):
                    r = batch_results[j]
                    if isinstance(r, Exception):
                        results_map[idx] = _err(str(r))
                    else:
                        results_map[idx] = r
                out = []
                for i in range(len(cmds)):
                    r = results_map[i]
                    out.append({"tab": cmds[i].get("tab", active_idx), **r})
                return {"ok": True, "result": json.dumps(out, indent=2)}

            # --- Daemon control ---
            if action == "ping":
                return _ok("pong")

            if action == "vnc_info":
                if not vnc_mode:
                    return _ok("VNC mode is not active. Restart with: browse start --vnc")
                info_lines = [
                    "VNC mode: ACTIVE",
                    f"Display: {VNC_DISPLAY}",
                    f"noVNC URL: http://localhost:{novnc_port}/vnc.html",
                    f"noVNC port: {novnc_port}",
                    f"VNC port: {VNC_PORT}",
                ]
                # Check if VNC subprocesses are alive
                alive = sum(1 for p in vnc_procs if p.poll() is None)
                info_lines.append(f"VNC processes: {alive}/{len(vnc_procs)} running")
                return _ok("\n".join(info_lines))

            if action == "stop":
                return {"ok": True, "result": "_SHUTDOWN_"}

            return _err(f"Unknown command: {action}")

        except Exception as e:
            return _err(f"{type(e).__name__}: {e}")

    # ----------------------------------------------------------
    # Socket server
    # ----------------------------------------------------------
    async def _handle(reader, writer):
        try:
            data = await asyncio.wait_for(reader.read(1048576), timeout=120)
            if not data:
                writer.close()
                return

            cmd = json.loads(data.decode())
            result = await _exec(cmd)

            if result.get("result") == "_SHUTDOWN_":
                writer.write(json.dumps(_ok("Browser stopped")).encode())
                await writer.drain()
                writer.close()
                await ctx.close()
                await pw.stop()
                _stop_vnc_services()
                SOCKET_PATH.unlink(missing_ok=True)
                PID_FILE.unlink(missing_ok=True)
                os._exit(0)

            writer.write(json.dumps(result).encode())
            await writer.drain()
        except Exception as e:
            try:
                writer.write(json.dumps(_err(str(e))).encode())
                await writer.drain()
            except Exception:
                pass
        finally:
            try:
                writer.close()
            except Exception:
                pass

    SOCKET_PATH.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(_handle, path=str(SOCKET_PATH))
    PID_FILE.write_text(str(os.getpid()))

    print(f"Browser daemon ready (PID {os.getpid()})", flush=True)

    try:
        await server.serve_forever()
    except asyncio.CancelledError:
        pass
    finally:
        server.close()
        _stop_vnc_services()
        SOCKET_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)


def _ok(msg: str) -> dict:
    return {"ok": True, "result": msg}


def _err(msg: str) -> dict:
    return {"ok": False, "error": msg}


def _trunc(s: str) -> str:
    return s[:MAX_TEXT] + "\n[... truncated ...]" if len(s) > MAX_TEXT else s


# ============================================================
# CLIENT — Send commands to daemon
# ============================================================

async def _send(cmd: dict, timeout: float = 120) -> dict:
    """Send command to daemon, return result."""
    try:
        reader, writer = await asyncio.open_unix_connection(str(SOCKET_PATH))
        writer.write(json.dumps(cmd).encode())
        await writer.drain()
        writer.write_eof()
        data = await asyncio.wait_for(reader.read(10485760), timeout=timeout)
        writer.close()
        return json.loads(data.decode())
    except (ConnectionRefusedError, FileNotFoundError):
        return _err("_NOT_RUNNING_")
    except asyncio.TimeoutError:
        return _err("Command timed out")
    except Exception as e:
        return _err(f"Connection error: {e}")


def _daemon_alive() -> bool:
    if not PID_FILE.exists():
        return False
    try:
        pid = int(PID_FILE.read_text().strip())
        os.kill(pid, 0)
        return SOCKET_PATH.exists()
    except (OSError, ValueError):
        PID_FILE.unlink(missing_ok=True)
        return False


async def _start_bg(vnc: bool = False, vnc_port: int = NOVNC_PORT) -> bool:
    """Start daemon as background process, wait until ready."""

    SOCKET_PATH.unlink(missing_ok=True)
    PID_FILE.unlink(missing_ok=True)

    # Set VNC flag file so daemon knows to start in VNC mode
    if vnc:
        VNC_FLAG_FILE.write_text(str(vnc_port) if vnc_port != NOVNC_PORT else "")
    else:
        VNC_FLAG_FILE.unlink(missing_ok=True)
        VNC_INFO_FILE.unlink(missing_ok=True)

    # Send daemon logs to a file so we can debug VNC startup issues
    log_path = DATA_DIR / "daemon.log"
    log_file = open(log_path, "w")

    subprocess.Popen(
        [sys.executable, __file__, "_daemon"],
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )

    # VNC mode needs more time to start Xvfb + x11vnc + websockify + Chromium
    max_wait = 60 if vnc else 40
    for _ in range(max_wait):
        await asyncio.sleep(0.5)
        if SOCKET_PATH.exists():
            r = await _send({"action": "ping"}, timeout=5)
            if r.get("ok"):
                return True
    return False


async def _ensure():
    """Ensure daemon is running."""
    if _daemon_alive():
        r = await _send({"action": "ping"}, timeout=5)
        if r.get("ok"):
            return True
    print("Starting browser...", file=sys.stderr)
    return await _start_bg()


async def _run(action: str, args: list):
    """Execute a command, auto-starting daemon if needed."""

    # Daemon management commands
    if action == "start":
        if _daemon_alive():
            print("Browser already running")
            return
        # Parse --vnc flag
        vnc = "--vnc" in args
        vnc_port = NOVNC_PORT
        if vnc:
            remaining = [a for a in args if a != "--vnc"]
            if remaining:
                try:
                    vnc_port = int(remaining[0])
                except ValueError:
                    pass
        ok = await _start_bg(vnc=vnc, vnc_port=vnc_port)
        if ok:
            msg = "Browser started"
            if vnc:
                msg += f" with VNC (noVNC: http://localhost:{vnc_port}/vnc.html)"
            print(msg)
        else:
            print("Failed to start browser")
            # Show daemon log for debugging
            log_path = DATA_DIR / "daemon.log"
            if log_path.exists():
                log = log_path.read_text()[-2000:]
                if log.strip():
                    print(f"Daemon log:\n{log}", file=sys.stderr)
        return

    if action == "stop":
        if not _daemon_alive():
            print("Browser not running")
            _stop_vnc_services()
            SOCKET_PATH.unlink(missing_ok=True)
            PID_FILE.unlink(missing_ok=True)
            return
        r = await _send({"action": "stop"})
        print(r.get("result", "Stopped"))
        return

    if action == "status":
        if _daemon_alive():
            pid = PID_FILE.read_text().strip()
            vnc_status = ""
            if VNC_INFO_FILE.exists():
                try:
                    info = json.loads(VNC_INFO_FILE.read_text())
                    vnc_status = f" | VNC active (noVNC: {info.get('novnc_url', '?')})"
                except (json.JSONDecodeError, KeyError):
                    pass
            print(f"Browser running (PID {pid}){vnc_status}")
        else:
            print("Browser not running")
        return

    if action == "vnc":
        if not _daemon_alive():
            print("Browser not running. Start with: browse start --vnc")
            return
        r = await _send({"action": "vnc_info"}, timeout=5)
        if r.get("ok"):
            print(r["result"])
        else:
            print(r.get("error", "Failed to get VNC info"))
        return

    if action == "restart":
        if _daemon_alive():
            await _send({"action": "stop"})
            await asyncio.sleep(1)
        SOCKET_PATH.unlink(missing_ok=True)
        PID_FILE.unlink(missing_ok=True)
        # Preserve --vnc flag on restart
        vnc = "--vnc" in args
        vnc_port = NOVNC_PORT
        if vnc:
            remaining = [a for a in args if a != "--vnc"]
            if remaining:
                try:
                    vnc_port = int(remaining[0])
                except ValueError:
                    pass
        ok = await _start_bg(vnc=vnc, vnc_port=vnc_port)
        msg = "Browser restarted" if ok else "Failed to restart"
        if ok and vnc:
            msg += f" with VNC (noVNC: http://localhost:{vnc_port}/vnc.html)"
        print(msg)
        return

    # All other commands: ensure daemon running, then execute
    await _ensure()

    timeout = 120
    if action in ("pfetch", "batch"):
        timeout = 300
    elif action == "fetch":
        timeout = 60

    r = await _send({"action": action, "args": args}, timeout=timeout)

    if r.get("ok"):
        print(r.get("result", "OK"))
    else:
        error = r.get("error", "Unknown error")
        if error == "_NOT_RUNNING_":
            # Daemon died, try restarting
            print("Browser crashed, restarting...", file=sys.stderr)
            if await _start_bg():
                r = await _send({"action": action, "args": args})
                if r.get("ok"):
                    print(r.get("result", "OK"))
                    return
            print("ERROR: Browser failed to recover", file=sys.stderr)
        else:
            print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)


# ============================================================
# CLI entry point
# ============================================================

HELP = """\
browse - Interactive browser tool for Saul Goodman

NAVIGATION:
  browse goto <url>                Navigate to URL
  browse back / forward / reload   Navigate history

PAGE INFO:
  browse snapshot                  Capture all elements with stable refs (e1, e2...)
  browse aria                      Full accessibility tree via CDP (semantic roles, states)
  browse describe                  Full page analysis (forms, buttons, links, text)
  browse text [sel|eN]             Get visible text
  browse title / url               Get page title or URL
  browse find <text>               Find elements containing text
  browse links [selector]          Get all links
  browse screenshot [name]         Save screenshot to disk
  browse fullshot [name]           Full-page screenshot to disk
  browse look                      Visual snapshot (inline base64 JPEG for vision)
  browse lookfull                  Full-page visual snapshot (inline base64 JPEG)

INTERACTION (all accept eN refs from snapshot):
  browse click <sel|eN>            Click element
  browse fill <sel|eN> <value>     Clear and fill input field
  browse type <sel|eN> <text>      Type keystroke by keystroke
  browse select <sel|eN> <value>   Select dropdown option
  browse check / uncheck <sel|eN>  Toggle checkbox
  browse press <key>               Keyboard key (Enter, Tab, Escape, ArrowDown...)
  browse hover <sel|eN>            Hover over element

WAITING:
  browse wait <sel|eN> [secs]      Wait for element (default 10s)
  browse sleep <seconds>           Pause

JAVASCRIPT:
  browse eval <code>               Run JS, return result

PAGES:
  browse pages                     List all open pages
  browse switch <index>            Switch active page
  browse new [url]                 Open new tab
  browse close                     Close current tab
  browse scroll [down|up] [px]     Scroll (default: down 500px)

PARALLEL BROWSING:
  browse fetch <url> [max_chars]   Open tab, extract text, close tab (one-shot)
  browse pfetch <url1> <url2> ...  Fetch multiple URLs in parallel tabs
         [--max-chars N]           (max 8 URLs, Cloudflare-aware, auto-cleanup)

COOKIES & STORAGE:
  browse cookies [domain]          Show cookies
  browse setcookie <n> <v> <dom>   Set a cookie
  browse exportcookies [file]      Export all cookies as JSON (to file or stdout)
  browse importcookies <file>      Import cookies from JSON or Netscape format file
  browse clearcookies [domain]     Clear all cookies (or only matching domain)
  browse localstorage list         List all localStorage key-value pairs
  browse localstorage get <key>    Get a localStorage value
  browse localstorage set <k> <v>  Set a localStorage value
  browse localstorage delete <key> Delete a localStorage key
  browse localstorage clear        Clear all localStorage
  browse sessionstorage list|get|set|delete|clear  Same for sessionStorage

FILES:
  browse upload <sel|eN> <path>    Upload file to input element
  browse downloads                 List captured downloads
  browse download <index>          Get info about a specific download
  browse waitdownload [secs]       Wait for a download to complete (default 30s)

DEBUGGING:
  browse console [count]           Show console messages (default 20)
  browse network [count] [filter]  Show network requests (optional URL filter)
  browse errors [count]            Show page errors only

DAEMON:
  browse start                     Start browser (headless)
  browse start --vnc [port]        Start with noVNC web viewer (default port 6080)
  browse stop                      Stop browser and VNC services
  browse restart [--vnc [port]]    Restart browser
  browse status                    Check if running (shows VNC info)
  browse vnc                       Show noVNC URL and VNC status

SELECTORS:
  e5                     Ref from snapshot (recommended)
  #id                    Element by ID
  .class                 Element by class
  [name="email"]         Element by attribute
  "Click me"             Plain text (for click command)
"""


def main():
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(HELP)
        return

    if sys.argv[1] == "_daemon":
        asyncio.run(_daemon_main())
        return

    action = sys.argv[1]
    args = sys.argv[2:]
    asyncio.run(_run(action, args))


if __name__ == "__main__":
    main()
