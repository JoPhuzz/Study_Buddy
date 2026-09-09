"""Turn an uploaded file into captures.

One rule decides everything here: **if the file already carries text, extract it.**
Reading a picture of words costs money, takes a model call, and can misread a digit;
lifting the words themselves is free, instant and exact. Vision is for what genuinely
has no text layer — a photo, a screenshot, a scanned page.

A PDF is therefore not one capture but several: each page becomes its own, in page
order, which is exactly the shape the rest of the app already expects. The reader's
continuation hint then does for a PDF what it does for a scrolled web page — it knows
page 4 follows page 3, and records what is new rather than repeating the header.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

# A page needs at least this much real text before we believe it has a text layer.
# Scanned pages often carry a stray character or two from a stamp or a page number.
MIN_PAGE_TEXT = 40
# Embedded images below this are furniture — logos, rules, bullets — not the page.
MIN_IMAGE_BYTES = 12_000
MAX_IMAGES_PER_PAGE = 2
MAX_UPLOAD_BYTES = 25 * 1024 * 1024

TEXT_SUFFIXES = (".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".yaml", ".yml",
                 ".log", ".rst", ".ini", ".toml", ".xml", ".srt", ".vtt")
IMAGE_SUFFIXES = (".png", ".jpg", ".jpeg", ".gif", ".webp")


class FileError(RuntimeError):
    """Something the user should see, phrased so they know what to do next."""


@dataclass
class Piece:
    """One thing to bank. Text OR an image, never both — that is the whole point."""
    label: str
    text: str = ""
    image: bytes = b""

    @property
    def is_text(self) -> bool:
        return bool(self.text.strip())


@dataclass
class Extraction:
    pieces: list[Piece] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)   # what the user should know about
    kind: str = ""


def _sniff(name: str, data: bytes) -> str:
    """Trust the bytes over the extension — a .txt that is really a PDF is still a PDF."""
    if data[:5] == b"%PDF-":
        return "pdf"
    if (data[:8] == b"\x89PNG\r\n\x1a\n" or data[:3] == b"\xff\xd8\xff" or data[:3] == b"GIF"
            or (data[:4] == b"RIFF" and data[8:12] == b"WEBP")):
        return "image"
    low = (name or "").lower()
    if low.endswith(IMAGE_SUFFIXES):
        return "image"
    if low.endswith(TEXT_SUFFIXES):
        return "text"
    # Anything that decodes cleanly and looks like prose is text, whatever it is called.
    try:
        s = data[:4096].decode("utf-8")
    except UnicodeDecodeError:
        return ""
    printable = sum(1 for c in s if c.isprintable() or c in "\n\r\t")
    return "text" if s and printable / len(s) > 0.92 else ""


def _pretty_text(name: str, data: bytes) -> str:
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        text = data.decode("utf-8", errors="replace")
    if (name or "").lower().endswith(".json"):
        try:                                   # pretty-print so structure survives reading
            text = json.dumps(json.loads(text), indent=2)
        except ValueError:
            pass
    return text.strip()


def _pdf_pieces(data: bytes, max_pages: int) -> Extraction:
    try:
        from pypdf import PdfReader
    except ImportError as exc:                 # pragma: no cover - packaging problem
        raise FileError("PDF support isn't installed on this server.") from exc
    import io

    out = Extraction(kind="pdf")
    try:
        reader = PdfReader(io.BytesIO(data))
        if getattr(reader, "is_encrypted", False):
            try:
                reader.decrypt("")             # many PDFs are "encrypted" with no password
            except Exception:
                raise FileError(
                    "That PDF is password-protected. Open it, save an unlocked copy, and "
                    "upload that.")
        pages = reader.pages
    except FileError:
        raise
    except Exception as exc:
        raise FileError(f"Couldn't open that PDF: {exc}") from exc

    total = len(pages)
    if total == 0:
        raise FileError("That PDF has no pages in it.")
    if total > max_pages:
        out.notes.append(
            f"{total} pages — only the first {max_pages} were read. Split it, or upload "
            "the section you actually need.")
    scanned = 0
    for i, page in enumerate(pages[:max_pages], start=1):
        label = f"page {i}"
        try:
            text = (page.extract_text() or "").strip()
        except Exception:
            text = ""
        if len(re.sub(r"\s+", "", text)) >= MIN_PAGE_TEXT:
            out.pieces.append(Piece(label=label, text=text))
            continue
        # No text layer. Bank the page's own artwork instead and let the reader look at it.
        imgs = []
        try:
            for im in page.images:
                blob = getattr(im, "data", b"") or b""
                if len(blob) >= MIN_IMAGE_BYTES:
                    imgs.append(blob)
        except Exception:
            imgs = []
        imgs.sort(key=len, reverse=True)
        if imgs:
            scanned += 1
            for blob in imgs[:MAX_IMAGES_PER_PAGE]:
                out.pieces.append(Piece(label=label, image=blob))
        else:
            out.notes.append(f"{label} had no readable text or image — skipped.")
    if scanned:
        out.notes.append(
            f"{scanned} page(s) had no text layer and were read as images, which costs a "
            "model call each and can misread small print.")
    if not out.pieces:
        raise FileError(
            "I couldn't get anything out of that PDF — no text layer and no readable page "
            "images. If it's a scan, a screenshot of it will work better.")
    return out


def extract(name: str, data: bytes, max_pages: int = 100) -> Extraction:
    """Split an uploaded file into the pieces to bank, in the order they should land."""
    if not data:
        raise FileError("That file is empty.")
    if len(data) > MAX_UPLOAD_BYTES:
        raise FileError(
            f"That file is {len(data) / 1_048_576:.0f} MB — the limit is "
            f"{MAX_UPLOAD_BYTES // 1_048_576} MB. For a big PDF, upload the section you need.")

    kind = _sniff(name, data)
    if kind == "pdf":
        return _pdf_pieces(data, max_pages)
    if kind == "image":
        return Extraction(kind="image", pieces=[Piece(label=name or "image", image=data)])
    if kind == "text":
        text = _pretty_text(name, data)
        if len(text) < 12:
            raise FileError("There's almost nothing in that file to read.")
        return Extraction(kind="text", pieces=[Piece(label=name or "text", text=text)])
    raise FileError(
        f"I don't know how to read {name or 'that file'}. I can take PDFs, images "
        "(PNG/JPEG/GIF/WebP) and text files (txt, md, csv, json, and similar). For "
        "anything else, put it on screen and capture it.")
