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

import re
import urllib.error
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
