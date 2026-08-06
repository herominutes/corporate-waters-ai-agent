"""File ingestion pipeline — extract text, chunk, embed, store in vector memory.

Handles file uploads from Telegram: PDF, DOCX, TXT, CSV, JSON, MD, and images.
Also handles YouTube URLs and X/Twitter video URLs — extracts transcripts and
ingests them. Chunks content semantically and stores each chunk as a memory with
embeddings for later retrieval via hybrid BM25 + cosine similarity search.
"""

import asyncio
import io
import json
import logging
import re
import subprocess
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)

# Injection scanning patterns
_INJECTION_PATTERNS = [
    # "Ignore/disregard previous" instructions
    re.compile(r'(?:ignore|disregard|forget|override)\s+(?:all\s+)?(?:previous|prior|above|earlier)\s+(?:instructions?|prompts?|rules?|context)', re.IGNORECASE),
    # Role-switching attempts
    re.compile(r'(?:you\s+are\s+now|act\s+as|pretend\s+to\s+be|new\s+instructions?:|system\s*:)', re.IGNORECASE),
    # Direct injection markers
    re.compile(r'<\s*(?:system|assistant|user)\s*>', re.IGNORECASE),
    # "Do not" overrides
    re.compile(r'(?:do\s+not|don\'t|never)\s+(?:mention|reveal|tell|show|disclose)\s+(?:that|this|these)', re.IGNORECASE),
]

# Invisible unicode characters used for injection
_INVISIBLE_CHARS = set([
    '​',  # zero-width space
    '‌',  # zero-width non-joiner
    '‍',  # zero-width joiner
    '⁠',  # word joiner
    '⁡',  # function application
    '⁢',  # invisible times
    '⁣',  # invisible separator
    '⁤',  # invisible plus
    '﻿',  # BOM / zero-width no-break space
    '­',  # soft hyphen
    '͏',  # combining grapheme joiner
    '؜',  # arabic letter mark
    'ᅟ',  # hangul choseong filler
    'ᅠ',  # hangul jungseong filler
    '឴',  # khmer vowel inherent aq
    '឵',  # khmer vowel inherent aa
    '᠎',  # mongolian vowel separator
])


def scan_for_injection(text: str, filename: str) -> list[str]:
    """Scan text for prompt injection patterns.

    Returns list of warning strings. Empty list = clean.
    """
    warnings = []

    # Check for invisible unicode
    invisible_count = sum(1 for c in text if c in _INVISIBLE_CHARS)
    if invisible_count > 5:
        warnings.append(f"Contains {invisible_count} invisible unicode characters (possible steganographic injection)")

    # Check for injection patterns
    for pattern in _INJECTION_PATTERNS:
        matches = pattern.findall(text)
        if matches:
            warnings.append(f"Suspicious pattern: '{matches[0]}' in {filename}")

    if warnings:
        log.warning("Injection scan warnings for '%s': %s", filename, "; ".join(warnings))

    return warnings


# Chunk config
CHUNK_SIZE = 500       # characters per chunk
CHUNK_OVERLAP = 50     # overlap between chunks
MAX_FILE_SIZE = 20_000_000  # 20 MB limit
SUPPORTED_EXTENSIONS = {
    ".pdf", ".docx", ".doc", ".pptx", ".txt", ".md", ".csv", ".json",
    ".py", ".js", ".ts", ".html", ".css", ".yaml", ".yml",
    ".toml", ".ini", ".cfg", ".log", ".xml", ".sql", ".epub",
    ".xlsx", ".xls",
}


def extract_text(file_bytes: bytes, filename: str) -> str | None:
    """Extract text content from file bytes based on file extension.

    Returns extracted text or None if unsupported/failed.
    """
    ext = Path(filename).suffix.lower()

    if ext == ".pdf":
        return _extract_pdf(file_bytes)
    elif ext == ".epub":
        return _extract_epub(file_bytes)
    elif ext == ".docx":
        return _extract_docx(file_bytes)
    elif ext == ".pptx":
        return _extract_pptx(file_bytes)
    elif ext == ".xlsx":
        return _extract_xlsx(file_bytes)
    elif ext == ".xls":
        return _extract_xls(file_bytes)
    elif ext in (".txt", ".md", ".csv", ".log", ".ini", ".cfg",
                  ".py", ".js", ".ts", ".html", ".css", ".yaml", ".yml",
                  ".toml", ".xml", ".sql", ".json"):
        return _extract_text(file_bytes)
    else:
        return None


def _extract_pdf(data: bytes) -> str | None:
    """Extract text from PDF bytes."""
    try:
        from PyPDF2 import PdfReader
        reader = PdfReader(io.BytesIO(data))
        pages = []
        for page in reader.pages:
            text = page.extract_text()
            if text:
                pages.append(text.strip())
        return "\n\n".join(pages) if pages else None
    except Exception as e:
        log.error("PDF extraction failed: %s", e)
        return None


def _extract_epub(data: bytes) -> str | None:
    """Extract text from EPUB bytes."""
    try:
        import zipfile
        from html.parser import HTMLParser

        class _HTMLTextExtractor(HTMLParser):
            def __init__(self):
                super().__init__()
                self._parts: list[str] = []
                self._skip = False

            def handle_starttag(self, tag, attrs):
                self._skip = tag in ("style", "script")
                if tag in ("p", "div", "br", "h1", "h2", "h3", "h4", "h5", "h6", "li", "tr"):
                    self._parts.append("\n")

            def handle_endtag(self, tag):
                if tag in ("style", "script"):
                    self._skip = False
                if tag in ("p", "div", "h1", "h2", "h3", "h4", "h5", "h6"):
                    self._parts.append("\n")

            def handle_data(self, data):
                if not self._skip:
                    self._parts.append(data)

            def get_text(self) -> str:
                return "".join(self._parts)

        with zipfile.ZipFile(io.BytesIO(data)) as zf:
            # Read spine order from content.opf
            opf_path = None
            # Find the OPF file from META-INF/container.xml
            try:
                container = zf.read("META-INF/container.xml").decode("utf-8")
                import re
                m = re.search(r'full-path="([^"]+)"', container)
                if m:
                    opf_path = m.group(1)
            except (KeyError, UnicodeDecodeError):
                pass

            # Collect XHTML/HTML content files in order
            html_files = []
            if opf_path:
                try:
                    opf_dir = str(Path(opf_path).parent)
                    opf_content = zf.read(opf_path).decode("utf-8")
                    # Extract manifest items (handle any attribute order)
                    manifest = {}
                    for item_m in re.finditer(r'<item\s+([^>]*?)/?>', opf_content):
                        attrs = item_m.group(1)
                        id_m = re.search(r'id="([^"]*)"', attrs)
                        href_m = re.search(r'href="([^"]*)"', attrs)
                        if id_m and href_m:
                            manifest[id_m.group(1)] = href_m.group(1)
                    # Extract spine order
                    for m in re.finditer(r'<itemref\s+[^>]*idref="([^"]*)"', opf_content):
                        href = manifest.get(m.group(1))
                        if href:
                            full = f"{opf_dir}/{href}" if opf_dir != "." else href
                            html_files.append(full)
                except Exception:
                    pass

            # Fallback: grab all xhtml/html files sorted by name
            if not html_files:
                html_files = sorted(
                    n for n in zf.namelist()
                    if n.endswith((".xhtml", ".html", ".htm"))
                    and "META-INF" not in n
                )

            chapters = []
            for name in html_files:
                try:
                    raw_bytes = zf.read(name)
                    # Try multiple encodings — Russian EPUBs often use cp1251
                    decoded = None
                    for enc in ("utf-8", "cp1251", "latin-1"):
                        try:
                            decoded = raw_bytes.decode(enc)
                            break
                        except UnicodeDecodeError:
                            continue
                    if not decoded:
                        continue
                    extractor = _HTMLTextExtractor()
                    extractor.feed(decoded)
                    text = extractor.get_text().strip()
                    if text:
                        chapters.append(text)
                except KeyError:
                    continue

        return "\n\n".join(chapters) if chapters else None
    except Exception as e:
        log.error("EPUB extraction failed: %s", e)
        return None


def _extract_docx(data: bytes) -> str | None:
    """Extract text from DOCX bytes."""
    try:
        from docx import Document
        doc = Document(io.BytesIO(data))
        paragraphs = [p.text for p in doc.paragraphs if p.text.strip()]
        return "\n\n".join(paragraphs) if paragraphs else None
    except Exception as e:
        log.error("DOCX extraction failed: %s", e)
        return None


def _extract_pptx(data: bytes) -> str | None:
    """Extract text from PPTX bytes: slide text, tables, and speaker notes."""
    try:
        from pptx import Presentation
        prs = Presentation(io.BytesIO(data))
        slides_out = []
        for i, slide in enumerate(prs.slides, 1):
            parts = []
            for shape in slide.shapes:
                if shape.has_text_frame:
                    text = shape.text_frame.text.strip()
                    if text:
                        parts.append(text)
                if getattr(shape, "has_table", False) and shape.has_table:
                    for row in shape.table.rows:
                        cells = [c.text.strip() for c in row.cells]
                        if any(cells):
                            parts.append("\t".join(cells))
            if slide.has_notes_slide:
                notes = slide.notes_slide.notes_text_frame.text.strip()
                if notes:
                    parts.append(f"(заметки докладчика: {notes})")
            if parts:
                slides_out.append(f"[Слайд {i}]\n" + "\n".join(parts))
        return "\n\n".join(slides_out) if slides_out else None
    except Exception as e:
        log.error("PPTX extraction failed: %s", e)
        return None


def _extract_xlsx(data: bytes) -> str | None:
    """Extract text from XLSX (Excel 2007+) bytes."""
    try:
        from openpyxl import load_workbook
        wb = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        sheets = []
        for ws in wb.worksheets:
            rows = []
            for row in ws.iter_rows(values_only=True):
                cells = [str(c) if c is not None else "" for c in row]
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                header = f"=== Sheet: {ws.title} ==="
                sheets.append(f"{header}\n{chr(10).join(rows)}")
        wb.close()
        return "\n\n".join(sheets) if sheets else None
    except Exception as e:
        log.error("XLSX extraction failed: %s", e)
        return None


def _extract_xls(data: bytes) -> str | None:
    """Extract text from XLS (Excel 97-2003) bytes."""
    try:
        import xlrd
        wb = xlrd.open_workbook(file_contents=data)
        sheets = []
        for ws in wb.sheets():
            rows = []
            for rx in range(ws.nrows):
                cells = [str(ws.cell_value(rx, cx)) for cx in range(ws.ncols)]
                if any(cells):
                    rows.append("\t".join(cells))
            if rows:
                header = f"=== Sheet: {ws.name} ==="
                sheets.append(f"{header}\n{chr(10).join(rows)}")
        return "\n\n".join(sheets) if sheets else None
    except Exception as e:
        log.error("XLS extraction failed: %s", e)
        return None


def _extract_text(data: bytes) -> str | None:
    """Extract text from plain text files (TXT, MD, CSV, code, etc.)."""
    for encoding in ("utf-8", "latin-1", "cp1251"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def chunk_text(text: str, chunk_size: int = CHUNK_SIZE,
               overlap: int = CHUNK_OVERLAP) -> list[str]:
    """Split text into overlapping chunks, preferring paragraph boundaries.

    Returns list of chunk strings.
    """
    if not text or not text.strip():
        return []

    # If text is short enough, return as single chunk
    if len(text) <= chunk_size:
        return [text.strip()]

    # Split on double newlines (paragraphs) first
    paragraphs = text.split("\n\n")
    chunks = []
    current = ""

    for para in paragraphs:
        para = para.strip()
        if not para:
            continue

        if len(current) + len(para) + 2 <= chunk_size:
            current = f"{current}\n\n{para}" if current else para
        else:
            if current:
                chunks.append(current.strip())
            # If a single paragraph is too long, split it by sentences
            if len(para) > chunk_size:
                sub_chunks = _split_long_paragraph(para, chunk_size, overlap)
                chunks.extend(sub_chunks)
                current = ""
            else:
                current = para

    if current.strip():
        chunks.append(current.strip())

    return chunks


def _split_long_paragraph(text: str, chunk_size: int,
                          overlap: int) -> list[str]:
    """Split a long paragraph into chunks at sentence boundaries."""
    import re
    sentences = re.split(r'(?<=[.!?])\s+', text)
    chunks = []
    current = ""

    for sentence in sentences:
        if len(current) + len(sentence) + 1 <= chunk_size:
            current = f"{current} {sentence}" if current else sentence
        else:
            if current:
                chunks.append(current.strip())
            current = sentence

    if current.strip():
        chunks.append(current.strip())

    return chunks


async def ingest_file(user_id: str, file_bytes: bytes, filename: str, visibility: str = "personal") -> dict:
    """Full pipeline: extract text -> chunk -> embed -> store in memory.

    visibility: "personal" (default, owner-only) or "shared" (Slack-accessible).

    Returns dict with status info:
        {"success": True, "filename": str, "chunks": int, "chars": int, "visibility": str}
        {"success": False, "error": str}
    """
    if visibility not in ("personal", "shared"):
        visibility = "personal"
    from goodman import db
    from goodman.embeddings import embed_batch, vec_to_bytes

    # Validate size
    if len(file_bytes) > MAX_FILE_SIZE:
        return {"success": False, "error": f"File too large ({len(file_bytes) / 1_000_000:.1f} MB, max 10 MB)"}

    # Extract text — try known parsers first, then fall back to raw text
    ext = Path(filename).suffix.lower()
    text = extract_text(file_bytes, filename)
    if not text or not text.strip():
        # Fallback: try reading as plain text regardless of extension
        if ext not in SUPPORTED_EXTENSIONS:
            text = _extract_text(file_bytes)
    if not text or not text.strip():
        return {"success": False, "error": "Could not extract text from file"}

    # Scan for injection attempts
    injection_warnings = scan_for_injection(text, filename)
    if injection_warnings:
        # Strip invisible characters but still ingest (with warning prefix)
        text = ''.join(c for c in text if c not in _INVISIBLE_CHARS)
        log.warning("File '%s' has injection warnings — invisible chars stripped, ingesting with caution", filename)

    total_chars = len(text)

    # Chunk
    chunks = chunk_text(text)
    if not chunks:
        return {"success": False, "error": "No content after chunking"}

    # Embed all chunks in batch
    vectors = embed_batch(chunks)

    # Store each chunk as a memory with [file:filename] prefix
    category = "document"
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        content = f"[file:{filename}] (chunk {i+1}/{len(chunks)}) {chunk}"
        vec_bytes = vec_to_bytes(vec)

        # Save directly with embedding (skip fuzzy dedup for file chunks)
        import aiosqlite
        from goodman.config import DB_PATH
        async with aiosqlite.connect(DB_PATH) as conn:
            # Check exact duplicate
            cursor = await conn.execute(
                "SELECT id FROM memories WHERE user_id = ? AND content = ?",
                (user_id, content),
            )
            if not await cursor.fetchone():
                await conn.execute(
                    "INSERT INTO memories (user_id, category, content, embedding, visibility) "
                    "VALUES (?, ?, ?, ?, ?)",
                    (user_id, category, content, vec_bytes, visibility),
                )
                await conn.commit()

    log.info("Ingested file '%s': %d chunks, %d chars, visibility=%s", filename, len(chunks), total_chars, visibility)
    return {
        "success": True,
        "filename": filename,
        "chunks": len(chunks),
        "chars": total_chars,
        "visibility": visibility,
    }


# --- YouTube transcript extraction ---

YOUTUBE_URL_RE = re.compile(
    r'(?:https?://)?(?:www\.)?(?:youtube\.com/watch\?v=|youtu\.be/|youtube\.com/shorts/)([\w-]{11})'
)


def extract_youtube_url(text: str) -> str | None:
    """Extract a YouTube video ID from text. Returns the full URL or None."""
    m = YOUTUBE_URL_RE.search(text)
    if m:
        return f"https://www.youtube.com/watch?v={m.group(1)}"
    return None


_BROWSE_COOKIE_PATH = Path(__file__).parent.parent.parent / "data" / "browser_profile" / "chromium_data"


def _extract_youtube_transcript(url: str) -> tuple[str | None, str, str]:
    """Extract transcript from YouTube using yt-dlp with browser cookies.

    Uses cookies from the stealth browser's Chromium profile (Google login).
    Falls back to no-cookie attempt if the browser profile is missing.

    Returns (transcript_text, video_title, video_id) or (None, "", "") on failure.
    """
    import glob
    import subprocess

    try:
        video_id = YOUTUBE_URL_RE.search(url).group(1)
        out_tpl = f"/tmp/yt_{video_id}"

        # Build cookie args (reused for both commands)
        cookie_args = []
        if _BROWSE_COOKIE_PATH.exists():
            cookie_args = ["--cookies-from-browser", f"chromium:{_BROWSE_COOKIE_PATH}"]

        # Step 1: Get title (--print skips downloads, so separate call)
        title_cmd = ["yt-dlp", "--js-runtimes", "node"] + cookie_args + [
            "--skip-download", "--print", "title", url,
        ]
        title_result = subprocess.run(
            title_cmd, capture_output=True, text=True, timeout=30,
        )
        title = title_result.stdout.strip().split("\n")[0] if title_result.stdout.strip() else video_id

        # Step 2: Download subtitles
        cmd = ["yt-dlp", "--js-runtimes", "node"] + cookie_args + [
            "--skip-download", "--write-subs", "--write-auto-subs",
            "--sub-langs", "en,ru,en-orig",
            "--output", out_tpl,
            url,
        ]
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=45)

        # Find downloaded subtitle files (yt-dlp names them as {output}.{lang}.{ext})
        sub_files = sorted(glob.glob(f"{out_tpl}.*.vtt")) + sorted(glob.glob(f"{out_tpl}.*.srt"))
        if not sub_files:
            stderr = result.stderr or ""
            if "Sign in to confirm" in stderr:
                log.warning("YouTube login required for %s — re-login via VNC needed", url)
            else:
                log.warning("No subtitles found for %s: %s", url, stderr[:200])
            return None, title, video_id

        # Pick best subtitle file: prefer manual en, then any en, then first
        sub_path = sub_files[0]
        for sf in sub_files:
            if ".en." in sf and "auto" not in sf.lower():
                sub_path = sf
                break

        raw = open(sub_path).read()

        # Clean VTT/SRT → plain text
        lines = []
        for line in raw.split("\n"):
            line = line.strip()
            if (not line or line.isdigit() or "-->" in line
                    or line.startswith(("WEBVTT", "Kind:", "Language:", "NOTE"))
                    or line.startswith("align:") or "position:" in line):
                continue
            # Strip inline VTT tags like <00:00:07.399><c>
            line = re.sub(r"<[^>]+>", "", line)
            line = line.strip()
            if not line:
                continue
            # Deduplicate consecutive lines (auto-subs repeat)
            if lines and line == lines[-1]:
                continue
            lines.append(line)

        transcript = " ".join(lines)
        transcript = re.sub(r"\s+", " ", transcript).strip()

        # Cleanup temp files
        import os
        for sf in glob.glob(f"{out_tpl}.*"):
            try:
                os.remove(sf)
            except OSError:
                pass

        if not transcript:
            log.warning("Subtitle file was empty after parsing for %s", url)
            return None, title, video_id

        log.info("YouTube transcript extracted for %s (%d chars)", url, len(transcript))
        return transcript, title, video_id

    except Exception as e:
        log.error("YouTube transcript extraction failed for %s: %s", url, e)
        return None, "", ""


async def ingest_youtube(user_id: str, url: str) -> dict:
    """Extract YouTube transcript and ingest into vector memory.

    Returns dict with status:
        {"success": True, "title": str, "chunks": int, "chars": int, "transcript": str}
        {"success": False, "error": str}
    """
    transcript, title, video_id = await asyncio.to_thread(_extract_youtube_transcript, url)

    if not transcript:
        return {"success": False, "error": "Could not extract transcript (no subtitles available)"}

    total_chars = len(transcript)
    label = f"YouTube: {title}"

    # Chunk the transcript
    chunks = chunk_text(transcript)
    if not chunks:
        return {"success": False, "error": "Transcript was empty after processing"}

    # Embed and store
    from goodman.embeddings import embed_batch, vec_to_bytes
    import aiosqlite
    from goodman.config import DB_PATH

    vectors = embed_batch(chunks)
    category = "document"

    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        content = f"[youtube:{title}] (chunk {i+1}/{len(chunks)}) {chunk}"
        vec_bytes = vec_to_bytes(vec)

        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT id FROM memories WHERE user_id = ? AND content = ?",
                (user_id, content),
            )
            if not await cursor.fetchone():
                await conn.execute(
                    "INSERT INTO memories (user_id, category, content, embedding, visibility) "
                    "VALUES (?, ?, ?, ?, 'personal')",
                    (user_id, category, content, vec_bytes),
                )
                await conn.commit()

    log.info("Ingested YouTube '%s': %d chunks, %d chars", title, len(chunks), total_chars)
    return {
        "success": True,
        "title": title,
        "video_id": video_id,
        "url": url,
        "chunks": len(chunks),
        "chars": total_chars,
        "transcript": transcript[:3000],  # First 3k chars for immediate context
    }


# --- X / Twitter video extraction ---

X_VIDEO_URL_RE = re.compile(
    r'https?://(?:www\.)?(?:x\.com|twitter\.com)/(\w+)/status/(\d+)'
)

_FFMPEG_PATH = Path.home() / "bin" / "ffmpeg"


def extract_x_url(text: str) -> str | None:
    """Extract an X/Twitter status URL from text. Returns the URL or None."""
    m = X_VIDEO_URL_RE.search(text)
    if m:
        return f"https://x.com/{m.group(1)}/status/{m.group(2)}"
    return None


def _extract_x_tweet_id(url: str) -> str | None:
    m = X_VIDEO_URL_RE.search(url)
    return m.group(2) if m else None


_SRC_DIR = str(Path(__file__).parent.parent)


def _transcribe_video_chunked(video_path: Path, work_dir: Path, ffmpeg: str) -> str | None:
    """Extract audio and transcribe in a separate process to isolate OOM risk."""
    env = {**__import__("os").environ, "PYTHONPATH": _SRC_DIR}
    try:
        result = subprocess.run(
            ["python3", "-m", "goodman._transcribe_video", str(video_path), "--ffmpeg", ffmpeg],
            capture_output=True, text=True, timeout=900, env=env,
        )
        if result.returncode != 0:
            log.error("Transcribe subprocess failed (rc=%d): %s", result.returncode, result.stderr[-500:])
            return None
        transcript = result.stdout.strip()
        return transcript if transcript else None
    except subprocess.TimeoutExpired:
        log.error("Transcribe subprocess timed out (15 min)")
        return None
    except Exception as e:
        log.error("Transcribe subprocess error: %s", e)
        return None


def _fetch_x_video_sync(tweet_id: str) -> tuple[str | None, str, str]:
    """Fetch video audio + tweet text from X syndication API. Returns (transcript, tweet_text, tweet_id)."""
    import httpx

    try:
        resp = httpx.get(
            f"https://cdn.syndication.twimg.com/tweet-result?id={tweet_id}&lang=en&token=x",
            headers={"User-Agent": "Mozilla/5.0"},
            timeout=15,
        )
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        log.error("X syndication API failed for %s: %s", tweet_id, e)
        return None, "", tweet_id

    tweet_text = data.get("text", "")
    user_name = data.get("user", {}).get("name", "")
    screen_name = data.get("user", {}).get("screen_name", "")

    media_details = data.get("mediaDetails", [])
    video_url = None
    for md in media_details:
        if md.get("type") != "video":
            continue
        video_info = md.get("video_info", {})
        variants = video_info.get("variants", [])
        mp4s = [v for v in variants if v.get("content_type") == "video/mp4"]
        if mp4s:
            video_url = max(mp4s, key=lambda v: v.get("bitrate", 0))["url"]
            break

    if not video_url:
        return None, tweet_text, tweet_id

    with tempfile.TemporaryDirectory() as td:
        video_path = Path(td) / "video.mp4"
        ffmpeg = str(_FFMPEG_PATH) if _FFMPEG_PATH.exists() else "ffmpeg"

        try:
            vresp = httpx.get(video_url, timeout=120, follow_redirects=True)
            vresp.raise_for_status()
            if len(vresp.content) > 500_000_000:
                log.warning("X video too large (%d MB), skipping", len(vresp.content) // 1_000_000)
                return None, tweet_text, tweet_id
            video_path.write_bytes(vresp.content)
        except Exception as e:
            log.error("X video download failed: %s", e)
            return None, tweet_text, tweet_id

        transcript = _transcribe_video_chunked(video_path, Path(td), ffmpeg)
        if not transcript:
            return None, tweet_text, tweet_id

    label = f"@{screen_name}" if screen_name else "unknown"
    full_text = ""
    if tweet_text:
        full_text += f"Tweet by {user_name} (@{screen_name}):\n{tweet_text}\n\n"
    if transcript:
        full_text += f"Video transcript:\n{transcript}"

    return full_text.strip() or None, tweet_text, tweet_id


async def ingest_x_video(user_id: str, url: str) -> dict:
    """Extract X video transcript and ingest into vector memory."""
    tweet_id = _extract_x_tweet_id(url)
    if not tweet_id:
        return {"success": False, "error": "Could not parse tweet ID from URL"}

    full_text, tweet_text, _ = await asyncio.to_thread(_fetch_x_video_sync, tweet_id)

    if not full_text:
        if tweet_text:
            return {
                "success": True, "has_video": False,
                "tweet_text": tweet_text, "transcript": tweet_text,
                "chunks": 0, "chars": len(tweet_text),
            }
        return {"success": False, "error": "Could not extract video or tweet text"}

    total_chars = len(full_text)
    chunks = chunk_text(full_text)
    if not chunks:
        return {"success": False, "error": "Content was empty after processing"}

    from goodman.embeddings import embed_batch, vec_to_bytes
    import aiosqlite
    from goodman.config import DB_PATH

    vectors = embed_batch(chunks)
    for i, (chunk, vec) in enumerate(zip(chunks, vectors)):
        content = f"[x-video:{tweet_id}] (chunk {i+1}/{len(chunks)}) {chunk}"
        vec_bytes = vec_to_bytes(vec)
        async with aiosqlite.connect(DB_PATH) as conn:
            cursor = await conn.execute(
                "SELECT id FROM memories WHERE user_id = ? AND content = ?",
                (user_id, content),
            )
            if not await cursor.fetchone():
                await conn.execute(
                    "INSERT INTO memories (user_id, category, content, embedding, visibility) "
                    "VALUES (?, ?, ?, ?, 'personal')",
                    (user_id, "document", content, vec_bytes),
                )
                await conn.commit()

    log.info("Ingested X video %s: %d chunks, %d chars", tweet_id, len(chunks), total_chars)
    return {
        "success": True, "has_video": True,
        "tweet_id": tweet_id, "url": url,
        "chunks": len(chunks), "chars": total_chars,
        "transcript": full_text[:3000],
    }
