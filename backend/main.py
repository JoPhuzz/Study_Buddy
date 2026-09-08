"""Study Buddy — FastAPI app.

You show it something across a series of deliberate captures; it reads each one as it
lands, compacts them into a brief when you say you're done, and then answers only from
that brief. One container: API plus the static frontend, for Railway.
"""
from __future__ import annotations

import base64
import hashlib
import threading
import time
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import fetch, modes, sync
from .config import config
from .llm import LLM, LLMError
from .store import Store
from .study import NoBrief, Study

ROOT = Path(__file__).resolve().parent.parent
FRONTEND = ROOT / "frontend"
DATA = Path(config.data_dir).expanduser()
BUILD = str(int(time.time()))

app = FastAPI(title=config.app_name)

_study: Study | None = None
_store: Store | None = None
_err = ""
_lock = threading.Lock()
_sync = sync.BrainSync(str(DATA), config.auto_push_seconds)
_boot: dict = {"pulled": None, "pull_error": "", "restored": False}


def _open_store() -> None:
    """Build the store + engine. Call with _lock held."""
    global _study, _store, _err
    try:
        _store = Store(config.db_path)
        llm = LLM(config.anthropic_api_key, config.deep_model, config.fast_model)
        _study = Study(llm, _store, app_name=config.app_name,
                       read_model=config.read_model())
        _err = ""
    except Exception as e:  # noqa: BLE001
        _study, _err = None, f"{type(e).__name__}: {e}"


def engine() -> Study:
    if _study is None:
        with _lock:
            if _study is None and not _err:
                _open_store()
    if _study is None:
        raise RuntimeError(_err or "engine unavailable")
    return _study


def load_brain() -> None:
    """Boot: pull the brain from the repo. Containers are ephemeral; this is what makes
    a brief you compacted last week still be there today."""
    if not sync.enabled():
        return
    try:
        archive = sync.fetch_brain()
        _boot["pulled"] = bool(archive)
        if archive:
            with _lock:
                if _store is not None:
                    _store.close()
                sync.unpack(archive, str(DATA))
                _boot["restored"] = True
        _sync.note_pulled(sync._current_sha())
    except Exception as e:  # noqa: BLE001
        _boot["pull_error"] = f"{type(e).__name__}: {e}"


@app.on_event("startup")
def _startup() -> None:
    load_brain()
    with _lock:
        _open_store()
    _sync.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    """A hard kill loses at most one debounce window; SIGTERM loses nothing."""
    try:
        _sync.push_now()
    finally:
        _sync.stop()


# --- auth: a single-password gate (it's on the open internet) ---
_SESSION = (hashlib.sha256(("study-buddy:" + config.access_password).encode()).hexdigest()
            if config.access_password else "")
_OPEN = {"/login", "/api/login", "/api/health"}


def _authed(request: Request) -> bool:
    return (not config.access_password) or request.cookies.get("sb_session") == _SESSION


@app.middleware("http")
async def auth_gate(request: Request, call_next):
    p = request.url.path
    if _authed(request) or p in _OPEN or p.startswith("/static"):
        return await call_next(request)
    if p.startswith("/api/"):
        return JSONResponse({"ok": False, "error": "Sign in first."}, status_code=401)
    return RedirectResponse("/login")


_NO_DIRTY = {"/api/login", "/api/sync/push"}


@app.middleware("http")
async def dirty_tracker(request: Request, call_next):
    resp = await call_next(request)
    p = request.url.path
    if (request.method in ("POST", "PUT", "DELETE") and p.startswith("/api/")
            and p not in _NO_DIRTY and resp.status_code < 400):
        _sync.mark_dirty()
    return resp


@app.middleware("http")
async def no_store(request: Request, call_next):
    """The page and its assets must never be cached, or the browser keeps running old JS
    after a deploy."""
    resp = await call_next(request)
    p = request.url.path
    if p in ("/", "/index.html", "/login") or p.startswith("/static"):
        resp.headers["Cache-Control"] = "no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
    return resp


@app.post("/api/login")
async def login(request: Request):
    body = await request.json()
    if config.access_password and (body.get("password") or "") == config.access_password:
        resp = JSONResponse({"ok": True})
        resp.set_cookie("sb_session", _SESSION, httponly=True, samesite="lax",
                        max_age=30 * 86400)
        return resp
    return JSONResponse({"ok": False, "error": "Wrong password."}, status_code=401)


@app.get("/login", response_class=HTMLResponse)
def login_page():
    return (FRONTEND / "login.html").read_text()


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    if not _authed(request):
        return RedirectResponse("/login")
    return (FRONTEND / "index.html").read_text().replace("__BUILD__", BUILD)


@app.get("/api/health")
def health():
    ok = _study is not None or not _err
    return {"ok": ok, "app": config.app_name, "issue": _err,
            "config": {"deep_model": config.deep_model, "fast_model": config.fast_model,
                       "read_tier": config.read_tier,
                       "anthropic_key_set": bool(config.anthropic_api_key),
                       "access_password_set": bool(config.access_password),
                       "knowledge_token_set": bool(config.knowledge_token),
                       "knowledge_subpath": config.knowledge_subpath,
                       "build": BUILD},
            "sync": _sync.status(), "boot": _boot}


def _b64_image(raw: str) -> bytes:
    s = (raw or "").split(",", 1)[-1]
    try:
        return base64.b64decode(s)
    except Exception:
        return b""


# --- the three operations -----------------------------------------------------------
@app.post("/api/capture")
async def capture(request: Request):
    """One press of the capture button. Reads the image and banks the notes."""
    body = await request.json()
    image = _b64_image(body.get("image") or "")
    if not image:
        return JSONResponse({"ok": False, "error": "No image in that capture."},
                            status_code=400)
    t0 = time.time()
    try:
        out = engine().capture(body.get("subject"), image, label=body.get("label") or "")
    except (LLMError, ValueError) as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    print(f"[capture] {time.time() - t0:.1f}s #{out['seq']} {out['subject']!r}")
    return {"ok": True, **out}


@app.post("/api/capture/url")
async def capture_url(request: Request):
    """Bank a page you named. Better than a screenshot for anything text-heavy: every
    line, in order, nothing lost at the fold."""
    body = await request.json()
    try:
        title, text = fetch.fetch(body.get("url") or "")
    except fetch.FetchError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    try:
        out = engine().capture_text(body.get("subject"), title, text,
                                    source=(body.get("url") or "").strip())
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    return {"ok": True, **out}


@app.post("/api/seal")
async def seal(request: Request):
    """"I'm done" — compact the banked captures into the brief."""
    body = await request.json() if await request.body() else {}
    t0 = time.time()
    try:
        out = engine().seal(body.get("subject"))
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except LLMError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    print(f"[seal] {time.time() - t0:.1f}s {out['n_shots']} shots {out['subject']!r}")
    return {"ok": True, **out}


@app.post("/api/ask")
async def ask(request: Request):
    body = await request.json()
    t0 = time.time()
    try:
        out = engine().ask(body.get("question") or "", body.get("subject"))
    except NoBrief as e:
        return JSONResponse({"ok": False, "error": str(e), "no_brief": True},
                            status_code=409)
    except ValueError as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)
    except Exception as e:  # noqa: BLE001
        return JSONResponse({"ok": False, "error": str(e)}, status_code=503)
    print(f"[ask] {time.time() - t0:.1f}s cached={out['cached']} {out['subject']!r}")
    return {"ok": True, **out}


# --- subjects, shots, brief ---------------------------------------------------------
@app.get("/api/state")
def state():
    eng = engine()
    subject = eng.store.current_subject()
    b = eng.store.get_brief(subject) if subject else None
    return {"ok": True, "subject": subject,
            "n_shots": eng.store.shot_count(subject) if subject else 0,
            "sealed": bool(b),
            "stale": bool(subject and eng.store.get_state(f"stale:{subject}") and b),
            "brief_shots": (b or {}).get("n_shots", 0),
            "mode": eng.mode().name, "modes": modes.catalog(),
            "n_turns": len(eng.store.turns(subject)) if subject else 0,
            "usage": eng.store.usage_summary(), "sync": _sync.status()}


@app.get("/api/subjects")
def subjects():
    return {"ok": True, "subjects": engine().store.subjects()}


@app.post("/api/subject")
async def set_subject(request: Request):
    body = await request.json()
    eng = engine()
    if body.get("rename"):
        new = eng.store.rename_subject(body.get("subject") or "", body["rename"])
        if not new:
            return JSONResponse({"ok": False, "error": "That name is taken."},
                                status_code=400)
        if eng.store.current_subject() is None:
            eng.store.set_current_subject(new)
        return {"ok": True, "subject": new}
    name = (body.get("subject") or "").strip()
    if not name:
        return JSONResponse({"ok": False, "error": "Give the subject a name."},
                            status_code=400)
    return {"ok": True, "subject": eng.store.set_current_subject(name)}


@app.post("/api/subject/new")
def new_subject():
    """Start fresh — the next capture names itself from what it sees."""
    engine().store.set_state("current_subject", "")
    return {"ok": True}


@app.delete("/api/subject")
async def delete_subject(request: Request):
    body = await request.json()
    name = (body.get("subject") or "").strip()
    eng = engine()
    ok = eng.store.delete_subject(name)
    if ok and (eng.store.current_subject() or "").lower() == name.lower():
        eng.store.set_state("current_subject", "")
    return {"ok": ok}


@app.get("/api/shots")
def shots(subject: str = ""):
    eng = engine()
    subject = subject.strip() or eng.store.current_subject() or ""
    return {"ok": True, "subject": subject, "shots": eng.store.shots(subject)}


@app.delete("/api/shots/{shot_id}")
def drop_shot(shot_id: int):
    eng = engine()
    ok = eng.store.drop_shot(shot_id)
    subject = eng.store.current_subject()
    if ok and subject:
        eng.store.set_state(f"stale:{subject}", "1")
    return {"ok": ok}


@app.get("/api/brief")
def brief(subject: str = ""):
    b = engine().brief(subject)
    return {"ok": True, "brief": b}


@app.get("/api/turns")
def turns(subject: str = ""):
    eng = engine()
    subject = subject.strip() or eng.store.current_subject() or ""
    return {"ok": True, "subject": subject, "turns": eng.store.turns(subject)}


@app.post("/api/turns/clear")
async def clear_turns(request: Request):
    body = await request.json() if await request.body() else {}
    eng = engine()
    subject = (body.get("subject") or "").strip() or eng.store.current_subject() or ""
    return {"ok": True, "cleared": eng.store.clear_turns(subject)}


@app.get("/api/mode")
def get_mode():
    eng = engine()
    return {"ok": True, "mode": eng.mode().name, "modes": modes.catalog()}


@app.post("/api/mode")
async def set_mode(request: Request):
    body = await request.json()
    name = engine().set_mode(body.get("mode") or "")
    if name is None:
        return JSONResponse({"ok": False, "error": "Unknown mode."}, status_code=400)
    return {"ok": True, "mode": name}


@app.get("/api/usage")
def usage():
    return {"ok": True, **engine().store.usage_summary()}


# --- brain ---------------------------------------------------------------------------
@app.get("/api/sync")
def sync_status():
    return {"ok": True, **_sync.status()}


@app.post("/api/sync/push")
def sync_push():
    return _sync.push_now(force=True)


app.mount("/static", StaticFiles(directory=str(FRONTEND)), name="static")


if __name__ == "__main__":
    uvicorn.run(app, host=config.host, port=config.port)
