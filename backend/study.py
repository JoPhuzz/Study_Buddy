"""The engine: capture → seal → ask.

Three operations, and the whole app is these three.

  capture()  reads ONE banked image (or a fetched page) into notes and files it against
             the subject. Called once per press of the capture button, so the reading
             happens while you carry on clicking rather than in one long wait at the end.
  seal()     compacts every note for the subject into the brief. This is "I'm done".
  ask()      answers from the brief and the conversation so far — and from nothing else.

The closed world is a property of this file's *structure*, not of its wording. ask()
builds its context from exactly two sources, the brief and the prior turns, and there is
no branch anywhere that can add a third. There is no search tool in the adapter to call,
no retrieval over anything but this subject, and no fallback that answers without a
brief. If you are extending this, that is the invariant to protect.
"""
from __future__ import annotations

import re

from . import prompts
from .llm import LLM, Answer, LLMError
from .modes import DEFAULT_MODE, MODES, Mode, get_mode
from .store import Store

MAX_HISTORY_TURNS = 12
READ_MAX_TOKENS = 1600      # a dense page of text needs room to be recorded properly
BRIEF_MAX_TOKENS = 8000     # the brief is the record; truncating it loses the material
SUMMARY_RE = re.compile(r"^\s*SUMMARY:\s*(.+?)\s*$", re.I | re.M)


class NoBrief(RuntimeError):
    """Asked a question before anything was sealed."""


def _split_summary(note: str) -> tuple[str, str]:
    """Pull the leading 'SUMMARY: ...' line out for the shot list; keep the full note."""
    m = SUMMARY_RE.search(note or "")
    if not m:
        first = (note or "").strip().split("\n", 1)[0]
        return first[:120], (note or "").strip()
    summary = m.group(1).strip()
    body = SUMMARY_RE.sub("", note, count=1).strip()
    return summary[:200], body or note.strip()


class Study:
    def __init__(self, llm: LLM, store: Store, app_name: str = "Study Buddy",
                 read_model: str | None = None) -> None:
        self.llm = llm
        self.store = store
        self.app_name = app_name
        self.read_model = read_model or llm.deep_model

    # --- helpers -------------------------------------------------------------------
    def _log(self, kind: str, ans: Answer) -> None:
        try:
            self.store.log_usage(kind, ans.model, ans.prompt_tokens, ans.completion_tokens,
                                 cost=ans.cost)
        except Exception:
            pass  # accounting must never break the thing being accounted for

    def mode(self) -> Mode:
        return get_mode(self.store.get_state("mode") or DEFAULT_MODE)

    def set_mode(self, name: str) -> str | None:
        key = (name or "").strip().lower()
        if key not in MODES:
            return None
        self.store.set_state("mode", key)
        return MODES[key].name

    # --- 1. capture ----------------------------------------------------------------
    def capture(self, subject: str | None, image: bytes, label: str = "") -> dict:
        """Read one banked image and file it. Returns what landed, for the shot list."""
        if not image:
            raise ValueError("No image in that capture.")
        subject = (subject or "").strip() or self.store.current_subject() or ""

        system = prompts.READER
        previous = self.store.last_shot(subject) if subject else None
        if previous and previous.get("summary"):
            system += prompts.CONTINUATION_HINT.format(previous=previous["summary"])

        user = "Record this capture."
        if label.strip():
            user += f" They labelled it: \"{label.strip()}\""
        ans = self.llm.ask(user=user, cached_system=prompts.READER,
                           system=system[len(prompts.READER):], images=[image],
                           model=self.read_model, max_tokens=READ_MAX_TOKENS)
        self._log("read", ans)
        summary, body = _split_summary(ans.text)

        if not subject:
            subject = self._name_subject(body, summary)
        subject = self.store.set_current_subject(subject)

        seq = self.store.add_shot(subject, note=body, summary=summary, label=label,
                                  kind="screen")
        # New material means the brief no longer describes everything. Say so rather
        # than answering from a brief that silently predates the last five captures.
        self.store.set_state(f"stale:{subject}", "1")
        last = self.store.last_shot(subject) or {}
        return {"subject": subject, "seq": seq, "id": last.get("id"), "summary": summary,
                "note": body, "cost": ans.cost, "n_shots": self.store.shot_count(subject)}

    def capture_text(self, subject: str | None, title: str, text: str,
                     source: str = "", kind: str = "url") -> dict:
        """File fetched page text as a capture. No vision call — the text IS the record,
        and it is a better one than any screenshot of the same page could be."""
        subject = (subject or "").strip() or self.store.current_subject() or title
        subject = self.store.set_current_subject(subject)
        # A banked page needs a summary you can scan. The title alone is fine for a web
        # page ("Bevel - Blender Manual") and useless for a PDF ("plans.pdf — page 3"),
        # so where the first line of content adds something, it goes in the label too.
        first = next((ln.strip() for ln in text.splitlines() if len(ln.strip()) > 3), "")
        summary = (title or "").strip() or (source or "pasted text")
        # A page title or filename is a weak label and gets the first line for help; a
        # title YOU typed is the label you chose, and stands alone.
        if (kind != "note" and first and first.lower() not in summary.lower()
                and summary.lower() not in first.lower()):
            summary = f"{summary} · {first}"
        summary = summary[:200]
        head = (f"WRITTEN BY YOU — your own words, not a captured source\nTITLE: {title}"
                if kind == "note" else f"SOURCE: {source}\nTITLE: {title}")
        note = f"{head}\n\n{text}".strip()
        seq = self.store.add_shot(subject, note=note, summary=summary, kind=kind,
                                  source=source)
        self.store.set_state(f"stale:{subject}", "1")
        last = self.store.last_shot(subject) or {}
        return {"subject": subject, "seq": seq, "id": last.get("id"), "summary": summary,
                "note": note, "cost": 0.0, "n_shots": self.store.shot_count(subject)}

    def write(self, subject: str | None, title: str, text: str) -> dict:
        """Bank something you typed yourself — a thought, a workflow, the thing you
        already know and want on the record next to what you captured.

        Nothing reads it: it goes in verbatim, because you are the author and there is
        no image to interpret. What matters is that it stays marked as YOURS all the
        way down. A pricing page and your opinion of the pricing page are both
        legitimate material, but an answer that blends the two — "the Team plan is the
        right one" stated as if the page said so — is the failure this app exists to
        prevent, and it would be your own words doing the blending.
        """
        text = (text or "").strip()
        if not text:
            raise ValueError("Nothing to bank — the note is empty.")
        title = (title or "").strip()
        if not title:
            title = next((ln.strip() for ln in text.splitlines() if ln.strip()), "Note")[:80]
        return self.capture_text(subject, title, text, source="", kind="note")

    def _name_subject(self, note: str, summary: str) -> str:
        """Name a subject from its first capture, so the flow never starts with a form."""
        try:
            ans = self.llm.ask(user=f"CAPTURE NOTES:\n{note[:3000]}",
                               system=prompts.NAME_SUBJECT,
                               model=self.llm.fast_model, max_tokens=40)
            self._log("name", ans)
            name = ans.text.strip().strip('"').strip()
            name = re.sub(r"\s+", " ", name)[:80]
            if name:
                return name
        except LLMError:
            pass
        return (summary or "Untitled subject")[:80]

    # --- 2. seal -------------------------------------------------------------------
    def seal(self, subject: str | None = None) -> dict:
        """"I'm done" — compact every note for the subject into the brief."""
        subject = (subject or "").strip() or self.store.current_subject() or ""
        if not subject:
            raise ValueError("Nothing has been captured yet.")
        shots = self.store.shots(subject)
        if not shots:
            raise ValueError(f"No captures banked for {subject} yet.")

        parts = []
        for s in shots:
            head = f"--- capture [{s['seq']}]"
            if s.get("label"):
                head += f' — labelled "{s["label"]}"'
            if s.get("kind") == "url" and s.get("source"):
                head += f" — fetched from {s['source']}"
            elif s.get("kind") == "note":
                head += " — WRITTEN BY THE PERSON THEMSELVES, in their own words"
            parts.append(f"{head} ---\n{s['note']}")
        notes = "\n\n".join(parts)

        system = prompts.BRIEF
        existing = self.store.get_brief(subject)
        user = f"SUBJECT: {subject}\n\nCAPTURE NOTES, in the order shown:\n\n{notes}"
        if existing and existing.get("text"):
            system += prompts.REBRIEF
            user = (f"SUBJECT: {subject}\n\nEXISTING BRIEF:\n\n{existing['text']}"
                    f"\n\n\nNOTES FROM CAPTURES SINCE:\n\n{notes}")

        ans = self.llm.ask(user=user, cached_system=prompts.BRIEF,
                           system=system[len(prompts.BRIEF):],
                           model=self.llm.deep_model, max_tokens=BRIEF_MAX_TOKENS)
        self._log("brief", ans)
        self.store.save_brief(subject, ans.text, len(shots))
        self.store.set_state(f"stale:{subject}", "")
        return {"subject": subject, "brief": ans.text, "n_shots": len(shots),
                "cost": ans.cost, "truncated": ans.truncated}

    def brief(self, subject: str | None = None) -> dict | None:
        subject = (subject or "").strip() or self.store.current_subject() or ""
        if not subject:
            return None
        b = self.store.get_brief(subject)
        if not b:
            return None
        return {"subject": subject, **b,
                "stale": bool(self.store.get_state(f"stale:{subject}")),
                "n_shots_now": self.store.shot_count(subject)}

    # --- 3. ask --------------------------------------------------------------------
    def ask(self, question: str, subject: str | None = None) -> dict:
        """Answer from the brief and the conversation. There is no third source."""
        question = (question or "").strip()
        if not question:
            raise ValueError("Ask me something about what you showed me.")
        subject = (subject or "").strip() or self.store.current_subject() or ""
        if not subject:
            raise NoBrief("Nothing captured yet — share a window and bank a few captures.")
        b = self.store.get_brief(subject)
        if not b:
            n = self.store.shot_count(subject)
            raise NoBrief(
                f"{n} capture{'s' if n != 1 else ''} banked for {subject}, but nothing "
                "compacted yet. Press Done and I'll read them into a brief first."
                if n else f"Nothing captured for {subject} yet.")

        mode = self.mode()
        identity = (prompts.IDENTITY_PLAIN if mode.plain else prompts.IDENTITY)
        cached = identity.format(app=self.app_name) + "\n\n" + mode.persona + prompts.CLOSED_WORLD
        if mode.quote_first is not False:
            cached += prompts.CITE

        # The brief goes in the CACHED half, not the tail. It is stable for as long as the
        # subject is sealed, and it is by far the largest thing here — a session is a long
        # run of questions against one unchanging document, which is exactly the shape
        # caching pays for. It also carries the prefix over Anthropic's minimum cacheable
        # length: the instructions alone are ~500 tokens, under the 1024 floor, so before
        # this the ask path declared a cache breakpoint that could never hit.
        cached += (f"\n\nTHE BRIEF — subject: {subject}, compacted from "
                   f"{b.get('n_shots', 0)} capture(s). This is everything you know:\n\n"
                   f"{b['text']}")

        # Only genuinely per-turn material goes after the breakpoint.
        tail = ""
        if self.store.get_state(f"stale:{subject}"):
            extra = self.store.shot_count(subject) - int(b.get("n_shots") or 0)
            if extra > 0:
                tail = (f"\n\nNOTE: {extra} capture(s) have been banked since this brief was "
                        "compacted and are NOT in it. If the question touches something they "
                        "might cover, say the brief predates them and they should press Done "
                        "to fold them in.")

        history = self.store.turns(subject)[-MAX_HISTORY_TURNS:]
        ans = self.llm.ask(user=question, cached_system=cached, system=tail,
                           history=history, model=self.llm.deep_model,
                           max_tokens=mode.max_tokens)
        self._log("ask", ans)
        self.store.add_turn(subject, question, ans.text)
        return {"subject": subject, "answer": ans.text, "model": ans.model,
                "cost": ans.cost, "mode": mode.name,
                "cached": bool(ans.cache_read), "truncated": ans.truncated}
