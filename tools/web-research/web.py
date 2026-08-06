"""Web browsing module for The Goodman Protocol.

Inspired by OpenClaw's layered approach:
  1. Lightweight HTTP fetch (httpx) — fast, no browser overhead
  2. Full Playwright browser — stealth Chrome with persistent profile
  3. Cloudflare-aware headers and error detection

Anti-detection strategy:
  - --disable-blink-features=AutomationControlled (hides navigator.webdriver)
  - Persistent user-data directory (real cookie/session persistence)
  - Current User-Agent string
  - Accept: text/markdown for Cloudflare cooperative mode
  - DuckDuckGo HTML-lite for search (no API key needed, bot-friendly)
"""

import asyncio
import json
import logging
import re
import time
import urllib.parse
from pathlib import Path

import httpx
from patchright.async_api import async_playwright, BrowserContext

logger = logging.getLogger(__name__)

# Persistent browser profile directory (survives restarts)
BROWSER_DATA_DIR = Path(__file__).parent.parent.parent / "data" / "browser_profile" / "chromium_data"
BROWSER_DATA_DIR.mkdir(parents=True, exist_ok=True)

# Current Chrome User-Agent (keep this updated periodically)
USER_AGENT = (
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36"
)

# Stealth Chrome flags
CHROME_ARGS = [
    "--disable-blink-features=AutomationControlled",  # Hide navigator.webdriver
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
# Defeats Cloudflare, DataDome, PerimeterX, and most bot detection.
STEALTH_JS = """
// 1. navigator.webdriver — the #1 detection vector
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });

// 2. window.chrome — headless Chrome lacks this entirely
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

// 3. Permissions API — headless returns different results
const originalQuery = window.navigator.permissions.query;
window.navigator.permissions.query = (parameters) => (
    parameters.name === 'notifications' ?
        Promise.resolve({ state: Notification.permission }) :
        originalQuery(parameters)
);

// 4. navigator.plugins — headless has empty array, real Chrome has 5
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

// 5. navigator.languages — ensure it's populated
Object.defineProperty(navigator, 'languages', { get: () => ['en-US', 'en'] });

// 6. WebGL vendor/renderer — headless shows "Google Inc. (Google)" which is a red flag
const getParameter = WebGLRenderingContext.prototype.getParameter;
WebGLRenderingContext.prototype.getParameter = function(parameter) {
    if (parameter === 37445) return 'Intel Inc.';           // UNMASKED_VENDOR_WEBGL
    if (parameter === 37446) return 'Intel Iris OpenGL Engine'; // UNMASKED_RENDERER_WEBGL
    return getParameter.call(this, parameter);
};

// 7. Codec detection — headless may differ
const origMediaCap = navigator.mediaCapabilities?.decodingInfo;
if (origMediaCap) {
    navigator.mediaCapabilities.decodingInfo = function(config) {
        return origMediaCap.call(this, config).then(result => {
            result.supported = true;
            result.smooth = true;
            result.powerEfficient = true;
            return result;
        });
    };
}

// 8. iframe contentWindow — headless leaks detection via cross-origin
// Patch to prevent "permission denied" errors that reveal automation
try {
    const origGetter = Object.getOwnPropertyDescriptor(HTMLIFrameElement.prototype, 'contentWindow').get;
    Object.defineProperty(HTMLIFrameElement.prototype, 'contentWindow', {
        get: function() {
            const result = origGetter.call(this);
            if (!result) return result;
            // Ensure toString doesn't leak
            if (!result.chrome) result.chrome = window.chrome;
            return result;
        }
    });
} catch(e) {}
"""

# Shared httpx client for lightweight fetches
_http_client: httpx.AsyncClient | None = None

# Patchright persistent context (CDP-patched Playwright — passes Cloudflare)
_context: BrowserContext | None = None
_playwright = None

# --- In-memory cache with TTL ---
_cache: dict[str, tuple[str, float]] = {}  # key -> (value, expires_at)
_CACHE_TTL = 900  # 15 minutes
_CACHE_MAX = 100


def _cache_get(key: str) -> str | None:
    """Get a cached value if it exists and hasn't expired."""
    entry = _cache.get(key)
    if entry is None:
        return None
    value, expires_at = entry
    if time.time() > expires_at:
        del _cache[key]
        return None
    return value


def _cache_set(key: str, value: str) -> None:
    """Store a value in cache with TTL. Evicts oldest if full."""
    if len(_cache) >= _CACHE_MAX:
        # Evict the entry closest to expiry
        oldest_key = min(_cache, key=lambda k: _cache[k][1])
        del _cache[oldest_key]
    _cache[key] = (value, time.time() + _CACHE_TTL)


# ------------------------------------------------------------------
# HTTP client (lightweight, no browser)
# ------------------------------------------------------------------

async def _get_http_client() -> httpx.AsyncClient:
    """Get or create the shared httpx client."""
    global _http_client
    if _http_client is None or _http_client.is_closed:
        _http_client = httpx.AsyncClient(
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/markdown, text/html;q=0.9, */*;q=0.1",
                "Accept-Language": "en-US,en;q=0.9",
            },
            follow_redirects=True,
            timeout=httpx.Timeout(30.0),
        )
    return _http_client


# ------------------------------------------------------------------
# Playwright browser (stealth Chrome with persistent profile)
# ------------------------------------------------------------------

async def _get_browser_context() -> BrowserContext:
    """Get or create a stealth browser context (patchright — CDP-patched Chromium)."""
    global _context, _playwright
    if _context:
        try:
            _context.pages
            return _context
        except Exception:
            _context = None

    _playwright = await async_playwright().start()
    _context = await _playwright.chromium.launch_persistent_context(
        user_data_dir=str(BROWSER_DATA_DIR),
        headless=True,
        args=CHROME_ARGS,
        user_agent=USER_AGENT,
        viewport={"width": 1280, "height": 900},
        locale="en-US",
        timezone_id="Africa/Cairo",
        ignore_https_errors=True,
    )
    # Inject stealth JS via CDP — using add_init_script() breaks DNS resolution
    # in patchright's persistent context (chromium bug with protocol interception)
    _init_page = _context.pages[0] if _context.pages else await _context.new_page()
    cdp = await _context.new_cdp_session(_init_page)
    await cdp.send("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
    await cdp.detach()
    logger.info("Patchright stealth browser launched at %s", BROWSER_DATA_DIR)
    return _context


def _extract_text_from_html(html: str) -> str:
    """Extract readable text from HTML (basic regex fallback)."""
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = text.replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>')
    text = text.replace('&quot;', '"').replace('&#39;', "'").replace('&nbsp;', ' ')
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def _html_to_markdown(html: str, url: str = "") -> tuple[str, str]:
    """Convert HTML to clean markdown using Readability + html2text.

    Returns (markdown_text, extraction_method).
    Falls back to regex if libraries fail.
    """
    # Try Readability first — extracts the main article content
    try:
        from readability import Document
        doc = Document(html, url=url)
        title = doc.short_title() or ""
        article_html = doc.summary()

        # Convert article HTML to markdown
        try:
            import html2text
            h = html2text.HTML2Text()
            h.body_width = 0  # Don't wrap lines
            h.ignore_images = False
            h.ignore_links = False
            h.ignore_emphasis = False
            h.skip_internal_links = True
            h.inline_links = True
            h.protect_links = True
            h.unicode_snob = True
            md = h.handle(article_html).strip()
            if len(md) > 100:
                if title and not md.startswith(f"# {title}"):
                    md = f"# {title}\n\n{md}"
                return md, "readability+markdown"
        except Exception as e:
            logger.debug("html2text failed: %s, falling back to text extraction", e)

        # html2text failed — extract plain text from Readability's cleaned HTML
        text = _extract_text_from_html(article_html)
        if len(text) > 100:
            if title:
                text = f"{title}\n\n{text}"
            return text, "readability+text"

    except Exception as e:
        logger.debug("Readability failed: %s, falling back to html2text on raw HTML", e)

    # Readability failed — try html2text directly on raw HTML
    try:
        import html2text
        h = html2text.HTML2Text()
        h.body_width = 0
        h.ignore_images = True
        h.skip_internal_links = True
        h.inline_links = True
        md = h.handle(html).strip()
        if len(md) > 100:
            return md, "html2text-raw"
    except Exception as e:
        logger.debug("html2text raw failed: %s", e)

    # Everything failed — regex fallback
    return _extract_text_from_html(html), "regex"


def _is_js_required(html: str) -> bool:
    """Detect pages that require JavaScript to render real content.

    Many sites return a 200 OK with a shell HTML page that says
    'Please enable JavaScript' — the actual content is rendered client-side.
    These pages are useless without a browser, so we detect them early
    and fall back to Playwright which CAN execute JavaScript.
    """
    lower = html.lower()

    # Common JavaScript-required indicators
    js_signals = [
        "please enable javascript",
        "javascript is required",
        "you need to enable javascript",
        "this site requires javascript",
        "this page requires javascript",
        "javascript must be enabled",
        "enable javascript to continue",
        "enable javascript to view",
        "javascript is disabled",
        "javascript is not enabled",
        "please turn on javascript",
        "activate javascript",
        "requires javascript to function",
        "this app requires javascript",
        "you must enable javascript",
        "browser does not support javascript",
        "noscript",  # <noscript> tag often contains "enable JS" messages
    ]

    # Count how many signals we find
    matches = sum(1 for sig in js_signals if sig in lower)

    # A single "noscript" tag is common even on working pages,
    # so require at least 2 signals OR one of the strong signals
    strong_signals = [
        "please enable javascript",
        "javascript is required",
        "enable javascript to continue",
        "this site requires javascript",
        "this page requires javascript",
    ]

    if any(sig in lower for sig in strong_signals):
        return True

    if matches >= 2:
        return True

    # Check for pages that are essentially empty shells with just a JS bundle
    # (very little visible text content relative to script content)
    visible_text = _extract_text_from_html(html)
    if len(html) > 1000 and len(visible_text.strip()) < 50:
        # Big HTML payload but almost no visible text — likely a JS app shell
        return True

    return False


def _is_cloudflare_block(status_code: int, text: str) -> bool:
    """Detect Cloudflare challenge/block pages."""
    cf_codes = {403, 503, 521, 522, 523, 524, 525, 526, 530}
    if status_code in cf_codes:
        lower = text.lower()
        if any(sig in lower for sig in [
            "cloudflare", "cf-browser-verification", "challenge-platform",
            "just a moment", "checking your browser", "ray id",
        ]):
            return True
    return False


# ------------------------------------------------------------------
# Public API: fetch_page
# ------------------------------------------------------------------

async def fetch_page(url: str, wait_for: str | None = None) -> str:
    """Fetch a web page. Tries lightweight HTTP first, falls back to browser."""
    cache_key = f"fetch:{url}"
    if not wait_for:
        cached = _cache_get(cache_key)
        if cached:
            logger.debug("Cache hit for %s", url)
            return cached

    t0 = time.time()

    # Layer 1: Try lightweight HTTP fetch first
    if not wait_for:
        result = await _fetch_http(url)
        if result:
            result = _add_metadata(result, time.time() - t0, "http")
            _cache_set(cache_key, result)
            return result

    # Layer 2: Fall back to full browser
    result = await _fetch_browser(url, wait_for)
    if result:
        result = _add_metadata(result, time.time() - t0, "browser")
        _cache_set(cache_key, result)
        return result

    # Layer 3: Cloudflare defeated the browser — try cached versions
    logger.info("Browser failed on %s, trying cached versions", url)
    result = await _fetch_cached(url)
    result = _add_metadata(result, time.time() - t0, "cached")
    _cache_set(cache_key, result)
    return result


def _add_metadata(result: str, elapsed: float, layer: str) -> str:
    """Add a compact metadata line to fetch results."""
    chars = len(result)
    truncated = "[... content truncated ...]" in result
    meta = f"[fetched via {layer} in {elapsed:.1f}s | {chars} chars"
    if truncated:
        meta += " | truncated"
    meta += "]"
    # Insert metadata after the URL/Page line
    lines = result.split("\n", 2)
    if len(lines) >= 2:
        return f"{lines[0]}\n{meta}\n{''.join(lines[1:])}"
    return f"{meta}\n{result}"


async def _fetch_http(url: str) -> str | None:
    """Lightweight HTTP fetch. Returns None if blocked or failed."""
    try:
        client = await _get_http_client()
        resp = await client.get(url)

        content_type = resp.headers.get("content-type", "")
        body = resp.text

        # Check for Cloudflare block
        if _is_cloudflare_block(resp.status_code, body):
            logger.info("Cloudflare block on %s, falling back to browser", url)
            return None

        if resp.status_code >= 400:
            logger.info("HTTP %d on %s, falling back to browser", resp.status_code, url)
            return None

        # Check for JavaScript-required pages BEFORE extracting text
        # These pages return 200 OK but contain no real content — just a JS loader
        if _is_js_required(body):
            logger.info("JS-required page detected on %s, falling back to browser", url)
            return None

        # If server returned markdown (Cloudflare cooperative mode), use it directly
        if "text/markdown" in content_type:
            text = body
            method = "cf-markdown"
            logger.info("Got markdown from %s (%d chars)", url, len(text))
        else:
            text, method = _html_to_markdown(body, url)

        if len(text) < 100:
            return None

        truncated = len(text) > 12000
        if truncated:
            text = text[:12000] + "\n\n[... content truncated ...]"

        logger.info("HTTP fetched %s via %s (%d chars)", url, method, len(text))
        return f"URL: {url}\n\n{text}"

    except Exception as e:
        logger.debug("HTTP fetch failed for %s: %s", url, e)
        return None


async def _click_turnstile(page) -> None:
    """Find and click a Cloudflare Turnstile checkbox if present on the page."""
    try:
        for frame in page.frames:
            if "challenges.cloudflare.com" not in frame.url:
                continue
            el = await frame.frame_element()
            if not el:
                continue
            bbox = await el.bounding_box()
            if not bbox or bbox["width"] == 0:
                continue
            # Click the checkbox area (left side of the Turnstile widget)
            cx = bbox["x"] + 25
            cy = bbox["y"] + bbox["height"] / 2
            logger.info("Clicking Turnstile checkbox at (%.0f, %.0f)", cx, cy)
            await page.mouse.click(cx, cy)
            await asyncio.sleep(2)
            return
    except Exception as e:
        logger.debug("Turnstile click failed: %s", e)


def _is_cloudflare_challenge_page(body_text: str) -> bool:
    """Detect Cloudflare challenge pages in browser-rendered content."""
    lower = body_text.lower().strip()
    if len(lower) > 500:
        return False  # Real pages are longer than challenge stubs
    return any(sig in lower for sig in [
        "just a moment",
        "checking your browser",
        "verify you are human",
        "security verification",
        "verifies you are not a bot",
        "cf-browser-verification",
        "challenge-platform",
        "enable javascript and cookies",
        "attention required",
        "checking if the site connection is secure",
        "performing security",
    ])


async def _fetch_browser(url: str, wait_for: str | None = None) -> str:
    """Full browser fetch with Playwright stealth Chrome."""
    try:
        ctx = await _get_browser_context()
        page = await ctx.new_page()

        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")

            if wait_for:
                await page.wait_for_selector(wait_for, timeout=10000)

            # Wait for JS rendering
            try:
                await page.wait_for_load_state("networkidle", timeout=10000)
            except Exception:
                await asyncio.sleep(3)

            # Check for Cloudflare challenge page — if detected, try to solve it
            body_text = await page.evaluate("() => document.body.innerText")
            if _is_cloudflare_challenge_page(body_text):
                logger.info("Cloudflare challenge on %s, attempting to solve...", url)
                # Try clicking Turnstile checkbox if present
                await _click_turnstile(page)
                # Poll for real content (challenge may auto-solve or redirect)
                for _ in range(8):
                    await asyncio.sleep(2)
                    body_text = await page.evaluate("() => document.body.innerText")
                    if not _is_cloudflare_challenge_page(body_text):
                        logger.info("Cloudflare challenge passed on %s", url)
                        break
                else:
                    logger.warning("Cloudflare challenge did not resolve on %s", url)
                    return None  # Signal to try cached fallback

            # Get full page HTML and convert to markdown
            page_html = await page.content()
            title = await page.title()
            content, method = _html_to_markdown(page_html, url)

            # If readability produced too little, fall back to innerText
            if len(content.strip()) < 100:
                content = await page.evaluate("""() => {
                    const remove = document.querySelectorAll(
                        'script, style, nav, footer, header, aside, [role="banner"], ' +
                        '[role="navigation"], .ad, .ads, .advertisement, .sidebar, ' +
                        '.cookie-banner, .popup, .modal'
                    );
                    remove.forEach(el => el.remove());
                    const main = document.querySelector(
                        'main, article, [role="main"], .content, #content, .post-content'
                    );
                    return (main || document.body).innerText;
                }""")
                method = "browser-innertext"

            truncated = len(content) > 12000
            if truncated:
                content = content[:12000] + "\n\n[... content truncated ...]"

            logger.info("Browser fetched %s via %s: '%s' (%d chars)", url, method, title, len(content))
            return f"Page: {title}\nURL: {url}\n\n{content}"

        finally:
            await page.close()

    except Exception as e:
        logger.error("Browser fetch failed for %s: %s", url, e)
        return f"Failed to load {url}: {e}"


async def _fetch_cached(url: str) -> str:
    """Try Web Archive and Google Cache when a site blocks us."""
    client = await _get_http_client()

    # Extract domain for content verification
    domain = urllib.parse.urlparse(url).netloc.replace("www.", "")

    # Try Web Archive first (more reliable)
    try:
        archive_url = f"https://web.archive.org/web/2/{url}"
        resp = await client.get(archive_url)
        if resp.status_code == 200:
            text, _ = _html_to_markdown(resp.text, url)
            if len(text) > 200:
                if len(text) > 12000:
                    text = text[:12000] + "\n\n[... content truncated ...]"
                logger.info("Got %d chars from Web Archive for %s", len(text), url)
                return f"Page (cached version from web.archive.org): {url}\n\n{text}"
    except Exception as e:
        logger.debug("Web Archive failed for %s: %s", url, e)

    # Try Google Cache
    try:
        cache_url = f"https://webcache.googleusercontent.com/search?q=cache:{url}"
        resp = await client.get(cache_url)
        if resp.status_code == 200:
            text, _ = _html_to_markdown(resp.text, url)
            if len(text) > 200 and domain in resp.text.lower():
                if len(text) > 12000:
                    text = text[:12000] + "\n\n[... content truncated ...]"
                logger.info("Got %d chars from Google Cache for %s", len(text), url)
                return f"Page (cached version from Google): {url}\n\n{text}"
    except Exception as e:
        logger.debug("Google Cache failed for %s: %s", url, e)

    return f"Failed to load {url}: blocked by Cloudflare and no cached version available"


# ------------------------------------------------------------------
# Public API: search_web
# ------------------------------------------------------------------

async def search_web(query: str, num_results: int = 5) -> str:
    """Search the web using DuckDuckGo HTML-lite (no API key needed)."""
    cache_key = f"search:{query}:{num_results}"
    cached = _cache_get(cache_key)
    if cached:
        logger.debug("Cache hit for search '%s'", query)
        return cached

    # Try DuckDuckGo HTML-lite first
    result = await _search_ddg(query, num_results)
    if result:
        _cache_set(cache_key, result)
        return result

    # Fallback: browser-based search
    result = await _search_browser(query)
    _cache_set(cache_key, result)
    return result


async def _search_ddg(query: str, num_results: int = 5) -> str | None:
    """Search via DuckDuckGo HTML-lite endpoint."""
    try:
        client = await _get_http_client()
        encoded = urllib.parse.quote_plus(query)
        resp = await client.get(
            f"https://html.duckduckgo.com/html/?q={encoded}",
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "text/html",
            },
        )

        if resp.status_code != 200:
            logger.debug("DuckDuckGo returned %d", resp.status_code)
            return None

        html = resp.text

        # Parse results from DDG HTML
        results = []
        # DDG HTML-lite uses class="result" for each result
        result_blocks = re.findall(
            r'<a[^>]*class="result__a"[^>]*href="([^"]*)"[^>]*>(.*?)</a>.*?'
            r'<a[^>]*class="result__snippet"[^>]*>(.*?)</a>',
            html, re.DOTALL
        )

        for href, title, snippet in result_blocks[:num_results]:
            # DDG wraps URLs in a redirect — extract the actual URL
            actual_url = href
            url_match = re.search(r'uddg=([^&]+)', href)
            if url_match:
                actual_url = urllib.parse.unquote(url_match.group(1))

            clean_title = _extract_text_from_html(title).strip()
            clean_snippet = _extract_text_from_html(snippet).strip()

            if clean_title:
                results.append(f"- {clean_title}\n  {actual_url}\n  {clean_snippet}")

        if not results:
            # Fallback: just extract all visible text
            text = _extract_text_from_html(html)
            if len(text) > 100:
                if len(text) > 5000:
                    text = text[:5000] + "\n\n[... truncated ...]"
                return f"Web search results for '{query}':\n\n{text}"
            return None

        return f"Web search results for '{query}':\n\n" + "\n\n".join(results)

    except Exception as e:
        logger.debug("DuckDuckGo search failed: %s", e)
        return None


async def _search_browser(query: str) -> str:
    """Fallback: browser-based search via DuckDuckGo."""
    try:
        ctx = await _get_browser_context()
        page = await ctx.new_page()

        try:
            encoded = urllib.parse.quote_plus(query)
            await page.goto(
                f"https://duckduckgo.com/?q={encoded}",
                timeout=30000, wait_until="domcontentloaded",
            )
            await asyncio.sleep(3)

            text = await page.evaluate("() => document.body.innerText")

            if not text or len(text) < 50:
                return f"No search results found for '{query}'."

            if len(text) > 5000:
                text = text[:5000] + "\n\n[... truncated ...]"

            return f"Web search results for '{query}':\n\n{text}"

        finally:
            await page.close()

    except Exception as e:
        logger.error("Browser search failed for '%s': %s", query, e)
        return f"Search failed: {e}"


# ------------------------------------------------------------------
# Public API: scrape_x_profile
# ------------------------------------------------------------------

async def scrape_x_profile(username: str) -> str:
    """Scrape recent posts from an X/Twitter profile (public, no login)."""
    try:
        ctx = await _get_browser_context()
        page = await ctx.new_page()

        try:
            url = f"https://x.com/{username}"
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            await asyncio.sleep(4)  # Let JS render tweets

            content = await page.evaluate("""() => {
                const tweets = document.querySelectorAll('[data-testid="tweetText"]');
                const results = [];
                tweets.forEach((t, i) => {
                    if (i < 10) results.push(t.innerText);
                });
                return results;
            }""")

            if content:
                lines = [f"Recent posts from @{username}:\n"]
                for i, tweet in enumerate(content, 1):
                    lines.append(f"{i}. {tweet}\n")
                return "\n".join(lines)

            return f"Could not fetch posts from @{username}. Profile may be private or X may require login."

        finally:
            await page.close()

    except Exception as e:
        logger.error("X scrape failed for @%s: %s", username, e)
        return f"Failed to scrape @{username}: {e}"


# ------------------------------------------------------------------
# Public API: take_screenshot
# ------------------------------------------------------------------

async def take_screenshot(url: str) -> str:
    """Take a screenshot of a page and save it."""
    try:
        ctx = await _get_browser_context()
        page = await ctx.new_page()

        try:
            await page.goto(url, timeout=30000, wait_until="domcontentloaded")
            screenshot_path = BROWSER_DATA_DIR / "last_screenshot.png"
            await page.screenshot(path=str(screenshot_path), full_page=False)
            return f"Screenshot saved: {screenshot_path}"
        finally:
            await page.close()

    except Exception as e:
        logger.error("Screenshot failed for %s: %s", url, e)
        return f"Screenshot failed: {e}"
