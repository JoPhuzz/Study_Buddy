"""Fetch a page YOU named and turn it into clean text.

This is the only code in the app that touches the open web, and it is not a search: it
retrieves one URL you typed and files the result as a capture like any other, visible in
the shot list and removable. Nothing here is ever called on the model's initiative.

It exists because screen-sharing gives the app pixels, never the DOM. For a program that
is the only option, but for a web page it is the worse one — a fetched article gives you
every line, in order, with nothing cut off at the fold, no OCR mistakes on a dense table,
and no twenty presses of the capture button to get down a long page. Reader mode in the
browser helps a screenshot; this skips the screenshot.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request

_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_MAX_BYTES = 3_000_000
_BLOCKED_MARKERS = ("just a moment", "enable javascript", "captcha",
                    "cf-browser-verification", "verify you are human", "access denied")


class FetchError(RuntimeError):
    """Something the user should see, phrased so they know what to do next."""


def looks_like_url(text: str) -> bool:
    t = (text or "").strip()
    return bool(re.match(r"^https?://\S+$", t, re.I))


# --- YouTube ------------------------------------------------------------------------
# id in the PATH: youtu.be/ID, /shorts/ID, /embed/ID, /v/ID, /live/ID
_YT_PATH_ID = re.compile(
    r"(?:youtu\.be/|youtube\.com/(?:embed/|shorts/|v/|live/))([A-Za-z0-9_-]{11})", re.I)
_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")

# How much speech goes in one capture. A video becomes several, the way a PDF becomes
# pages: the shot list stays navigable by timestamp, and a section you don't care about
# can be binned without re-fetching the rest.
SEGMENT_SECONDS = 600
MIN_SPLIT_SECONDS = 780      # shorter than this stays a single capture
STAMP_EVERY = 30             # a timestamp roughly this often, so a quote can be found


def youtube_id(url: str) -> str | None:
    """The 11-char video id, robust to real share URLs — mobile links, tracking params
    before `v=`, youtu.be, /shorts/, /embed/, /live/."""
    url = url or ""
    m = _YT_PATH_ID.search(url)
    if m:
        return m.group(1)
    try:
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
    except ValueError:
        return None
    for v in q.get("v", []):
        if _ID_RE.match(v):
            return v
    return None


def _stamp(seconds: float) -> str:
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    return f"{h}:{m:02d}:{sec:02d}" if h else f"{m}:{sec:02d}"


def _youtube_title(vid: str) -> str:
    try:
        u = ("https://www.youtube.com/oembed?url=https://www.youtube.com/watch?v="
             f"{vid}&format=json")
        with urllib.request.urlopen(u, timeout=10) as r:
            data = json.loads(r.read())
        title = (data.get("title") or "").strip()
        author = (data.get("author_name") or "").strip()
        return f"{title} — {author}" if title and author else (title or f"YouTube {vid}")
    except Exception:
        return f"YouTube {vid}"


def _snippets(vid: str) -> list[tuple[float, str]]:
    """(start_seconds, text) for the whole transcript.

    YouTube keeps changing this endpoint, so both the 1.x instance `.fetch()` and the
    legacy 0.6 static `.get_transcript()` are tried before giving up.
    """
    try:
        from youtube_transcript_api import YouTubeTranscriptApi as YT
    except ImportError as exc:  # pragma: no cover - packaging problem
        raise FetchError("YouTube support isn't installed on this server.") from exc

    out: list[tuple[float, str]] = []
    try:
        fetched = YT().fetch(vid)
        for sn in fetched:
            text = (getattr(sn, "text", "") or "").strip()
            if text:
                out.append((float(getattr(sn, "start", 0.0) or 0.0), text))
    except Exception as first:
        if hasattr(YT, "get_transcript"):
            try:
                for sn in YT.get_transcript(vid):
                    text = (sn.get("text") or "").strip()
                    if text:
                        out.append((float(sn.get("start") or 0.0), text))
            except Exception:
                pass
        if not out:
            raise FetchError(
                "Couldn't get a transcript for that video — captions may be turned off, "
                "or YouTube blocked the request. If it's your own recording, paste the "
                "text instead; otherwise capture the video's page from your screen."
            ) from first
    if not out:
        raise FetchError(
            "That video has no transcript — captions are probably disabled on it.")
    return out


def youtube(url: str) -> tuple[str, list[tuple[str, str]]]:
    """(title, [(label, text), ...]) — one entry per segment, in order.

    Timestamps are kept in the text on purpose. "Where does it say that?" is a question
    people actually ask of a video, and an answer of "at 12:40" is worth far more than
    the same sentence with no way back to it.
    """
    vid = youtube_id(url)
    if not vid:
        raise FetchError("That doesn't look like a YouTube link.")
    snips = _snippets(vid)
    title = _youtube_title(vid)
    total = snips[-1][0] if snips else 0.0

    segments: list[tuple[str, str]] = []
    bucket_start = 0.0
    lines: list[str] = []
    last_stamp = -STAMP_EVERY

    def flush(end: float) -> None:
        if not lines:
            return
        label = (f"{_stamp(bucket_start)}–{_stamp(end)}"
                 if total >= MIN_SPLIT_SECONDS else "transcript")
        segments.append((label, " ".join(lines).strip()))

    for start, text in snips:
        if total >= MIN_SPLIT_SECONDS and start - bucket_start >= SEGMENT_SECONDS:
            flush(start)
            lines, bucket_start, last_stamp = [], start, start - STAMP_EVERY
        if start - last_stamp >= STAMP_EVERY:
            lines.append(f"[{_stamp(start)}]")
            last_stamp = start
        lines.append(text)
    flush(total)
    if not segments:
        raise FetchError("That transcript came back empty.")
    return title, segments


def _title_from_html(html: str, url: str) -> str:
    m = re.search(r"<title[^>]*>(.*?)</title>", html, re.I | re.S)
    if m:
        title = re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()
        if title:
            return title[:200]
    return url.split("/")[-1] or url


def fetch(url: str) -> tuple[str, str]:
    """(title, clean text) for a URL. Raises FetchError with something actionable."""
    url = (url or "").strip()
    if not looks_like_url(url):
        raise FetchError("That doesn't look like a URL — it needs to start with http.")
    req = urllib.request.Request(url, headers={"User-Agent": _UA,
                                               "Accept": "text/html,*/*"})
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            raw = r.read(_MAX_BYTES)
            charset = (r.headers.get_content_charset() or "utf-8")
    except urllib.error.HTTPError as exc:
        raise FetchError(
            f"That page returned {exc.code}. If it needs a login, capture it from your "
            "screen instead — you're signed in there and I'm not.") from exc
    except Exception as exc:
        raise FetchError(f"Couldn't reach that page: {exc}") from exc

    html = raw.decode(charset, errors="replace")
    low = html[:4000].lower()
    if any(m in low for m in _BLOCKED_MARKERS):
        raise FetchError(
            "That site blocked me or renders entirely in the browser. Capture it from "
            "your screen instead — turn on your browser's Reader view first and it'll "
            "read cleanly.")

    title = _title_from_html(html, url)
    try:
        import trafilatura
        text = trafilatura.extract(html, include_tables=True, include_links=False,
                                   favor_recall=True) or ""
    except ImportError:
        text = ""
    if not text.strip():
        text = _crude_text(html)
    text = text.strip()
    if len(text) < 40:
        raise FetchError(
            "I fetched that page but found almost no text on it — it's probably built by "
            "JavaScript. Capture it from your screen instead.")
    return title, text


def _crude_text(html: str) -> str:
    """Fallback when trafilatura isn't installed or finds nothing: strip the markup."""
    html = re.sub(r"(?is)<(script|style|noscript|svg)[^>]*>.*?</\1>", " ", html)
    html = re.sub(r"(?i)<br\s*/?>|</p>|</div>|</li>|</tr>|</h[1-6]>", "\n", html)
    text = re.sub(r"<[^>]+>", " ", html)
    text = (text.replace("&nbsp;", " ").replace("&amp;", "&").replace("&lt;", "<")
                .replace("&gt;", ">").replace("&quot;", '"').replace("&#39;", "'"))
    text = re.sub(r"[ \t]+", " ", text)
    return re.sub(r"\n\s*\n\s*\n+", "\n\n", text).strip()
