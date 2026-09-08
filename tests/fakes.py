"""Test doubles — no network, no API key, no screen."""
from __future__ import annotations

from backend.llm import Answer


class FakeLLM:
    """Records every call and routes replies by which prompt it was handed."""

    deep_model = "deep-model"
    fast_model = "fast-model"

    def __init__(self, read_reply: str = "SUMMARY: a screen\nSome recorded detail.",
                 brief_reply: str = "# The Subject\nEverything, compacted.",
                 answer_reply: str = "an answer",
                 name_reply: str = "Named Subject") -> None:
        self.read_reply = read_reply
        self.brief_reply = brief_reply
        self.answer_reply = answer_reply
        self.name_reply = name_reply
        self.calls: list[dict] = []

    def ask(self, *, user, system="", cached_system="", images=None, history=None,
            model=None, max_tokens=800) -> Answer:
        rec = {"user": user, "system": system, "cached_system": cached_system,
               "full_system": (cached_system or "") + (system or ""),
               "images": list(images or []), "history": list(history or []),
               "model": model, "max_tokens": max_tokens}
        self.calls.append(rec)
        full = rec["full_system"]
        if "You are recording ONE capture" in full:
            rec["kind"] = "read"
            text = self.read_reply
        elif "compacting a series of captures" in full:
            rec["kind"] = "brief"
            text = self.brief_reply
        elif "Name this study subject" in full:
            rec["kind"] = "name"
            text = self.name_reply
        else:
            rec["kind"] = "ask"
            text = self.answer_reply
        return Answer(text=text, model=model or "deep-model", prompt_tokens=10,
                      completion_tokens=5, cost=0.001)

    def of_kind(self, kind: str) -> list[dict]:
        return [c for c in self.calls if c.get("kind") == kind]
