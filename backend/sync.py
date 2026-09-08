"""The brain: one SQLite file, kept in a private GitHub repo.

Railway containers keep no disk, so without this every brief you compacted would vanish
on the next deploy. The database is pushed as a tar.gz to KNOWLEDGE_SUBPATH in the
buddies-backups repo: pulled once at boot, marked dirty on any write, and pushed on a
debounce and again on shutdown.

Carried over from Program Buddy, including the one hard-won rule: a push that finds the
remote has moved on REFUSES rather than overwriting. That check exists because a
shutting-down container once flushed its stale in-memory copy over a repair someone had
pushed three minutes earlier.

This head assumes it is the only writer of its file. Do not run two instances against
one KNOWLEDGE_SUBPATH.
"""
from __future__ import annotations

import base64
import io
import json
import re
import tarfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from .config import config

BRAIN_FILE = "study.db"


def enabled() -> bool:
    return bool(config.knowledge_token and config.knowledge_repo)


def _repo() -> tuple[str, str]:
    m = re.search(r"github\.com[:/]+([^/]+)/([^/.]+)", config.knowledge_repo or "")
    if not m:
        raise RuntimeError("KNOWLEDGE_REPO must be a github repo URL.")
    return m.group(1), m.group(2)


def _api(path: str, method: str = "GET", payload: dict | None = None,
         accept: str = "application/vnd.github+json") -> tuple[int, bytes]:
    owner, repo = _repo()
    url = f"https://api.github.com/repos/{owner}/{repo}/{path.lstrip('/')}"
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method,
                                 headers={"Accept": accept, "User-Agent": "study-buddy",
                                          "Content-Type": "application/json"})
    if config.knowledge_token:
        req.add_header("Authorization", f"Bearer {config.knowledge_token}")
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.status, r.read()


def fetch_brain() -> bytes | None:
    """The raw tar.gz, or None if there isn't one yet.

    A fine-grained PAT scoped outside its repo list returns 404 — identical to "not there
    yet" — so a missing brain and a mis-scoped token look the same from here. /api/health
    surfaces the push error, which is where the difference shows up.
    """
    try:
        _, body = _api(f"contents/{urllib.parse.quote(config.knowledge_subpath)}",
                       accept="application/vnd.github.raw+json")
        return body
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise


def _current_sha() -> str | None:
    """The blob sha of the brain file. Listing the parent works at any file size, where a
    direct JSON GET fails past 1 MB."""
    parent = str(Path(config.knowledge_subpath).parent).replace("\\", "/")
    name = Path(config.knowledge_subpath).name
    try:
        _, body = _api(f"contents/{urllib.parse.quote(parent)}")
        for it in json.loads(body):
            if isinstance(it, dict) and it.get("name") == name:
                return it.get("sha")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return None
        raise
    return None


def pack(data_dir: str, now: float | None = None) -> bytes:
    now = time.time() if now is None else now
    d = Path(data_dir).expanduser()
    manifest = {"version": 1, "created": now, "app": config.app_name,
                "files": [BRAIN_FILE] if (d / BRAIN_FILE).exists() else []}
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        mb = json.dumps(manifest, indent=2).encode()
        info = tarfile.TarInfo("manifest.json")
        info.size, info.mtime = len(mb), int(now)
        tar.addfile(info, io.BytesIO(mb))
        if (d / BRAIN_FILE).exists():
            tar.add(d / BRAIN_FILE, arcname=BRAIN_FILE)
    return buf.getvalue()


def unpack(archive: bytes, data_dir: str) -> bool:
    d = Path(data_dir).expanduser()
    d.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        names = tar.getnames()
        if "manifest.json" not in names:
            raise ValueError("Not a brain archive (no manifest).")
        member = tar.getmember(BRAIN_FILE) if BRAIN_FILE in names else None
        if member is None:
            return False
        member.name = BRAIN_FILE          # never let an archive path escape data_dir
        tar.extract(member, path=str(d))
    return True


class BrainConflict(RuntimeError):
    """The repo's brain moved since we pulled it — pushing would destroy that change."""


def push(archive: bytes, message: str = "study-web: brain sync",
         base_sha: str | None = None) -> str:
    remote = _current_sha()
    if base_sha is not None and remote and remote != base_sha:
        raise BrainConflict(
            f"the brain in the repo changed since this instance loaded it "
            f"(expected {base_sha[:12]}, found {remote[:12]})")
    payload = {"message": message, "content": base64.b64encode(archive).decode()}
    if remote:
        payload["sha"] = remote
    _, body = _api(f"contents/{urllib.parse.quote(config.knowledge_subpath)}", "PUT", payload)
    try:
        return (json.loads(body).get("content") or {}).get("sha", "") or ""
    except Exception:
        return ""


class BrainSync:
    """Pull at boot, mark dirty on writes, debounce-push in the background."""

    def __init__(self, data_dir: str, interval: int) -> None:
        self.data_dir = data_dir
        self.interval = max(0, int(interval))
        self._dirty = False
        self._lock = threading.Lock()
        self._last_push: float | None = None
        self._last_pull: float | None = None
        self._error = ""
        self._conflict = False
        self._base_sha: str | None = None
        self._stop = threading.Event()

    def note_pulled(self, sha: str | None) -> None:
        self._base_sha = sha
        self._last_pull = time.time()

    def mark_dirty(self) -> None:
        with self._lock:
            self._dirty = True

    @property
    def dirty(self) -> bool:
        return self._dirty

    def push_now(self, force: bool = False) -> dict:
        if not enabled():
            return {"ok": False, "error": "sync disabled (no KNOWLEDGE_TOKEN)"}
        with self._lock:
            if not (self._dirty or force):
                return {"ok": True, "skipped": "nothing changed"}
        try:
            sha = push(pack(self.data_dir), base_sha=self._base_sha)
            with self._lock:
                self._dirty = False
                self._last_push = time.time()
                self._base_sha = sha or self._base_sha
                self._error, self._conflict = "", False
            return {"ok": True, "sha": sha}
        except BrainConflict as exc:
            with self._lock:
                self._error, self._conflict = str(exc), True
            return {"ok": False, "error": str(exc), "conflict": True}
        except Exception as exc:
            with self._lock:
                self._error = f"{type(exc).__name__}: {exc}"
            return {"ok": False, "error": self._error}

    def _loop(self) -> None:
        while not self._stop.wait(min(self.interval, 60) or 60):
            if self._dirty and self.interval:
                last = self._last_push or 0
                if time.time() - last >= self.interval:
                    self.push_now()

    def start(self) -> None:
        if enabled() and self.interval:
            threading.Thread(target=self._loop, daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def status(self) -> dict:
        return {"enabled": enabled(), "dirty": self._dirty, "conflict": self._conflict,
                "last_pushed": self._last_push, "last_pulled": self._last_pull,
                "error": self._error, "interval": self.interval}
