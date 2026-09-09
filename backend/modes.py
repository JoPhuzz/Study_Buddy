"""Modes — how an answer drawn from the brief is pitched.

Every mode here is closed-world. There is deliberately no mode that may consult the web
or the model's own knowledge of the subject, and no flag that would enable one: the gate
lives in study.ask(), which builds its context from the brief and the conversation and
has no branch for anything else. A mode changes tone and length, never sources.
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Mode:
    name: str
    max_tokens: int
    persona: str
    quote_first: bool | None = None   # None/True = cite capture numbers; False = don't
    plain: bool = False               # drop the warm identity


MODES: dict[str, Mode] = {
    "answer": Mode(
        name="Answer", max_tokens=500,
        persona=(
            "Answer the question from the brief, directly and in as few words as it "
            "honestly takes. Lead with the answer itself, then any qualifier that "
            "genuinely changes it. If the brief doesn't cover it, say so in your first "
            "sentence rather than working up to it."
        ),
    ),
    "quote": Mode(
        name="Quote", max_tokens=600, quote_first=True,
        persona=(
            "They need the source's own words, not your paraphrase — this is for terms, "
            "prices, limits and anything they may have to rely on. Give the exact wording "
            "from the brief in quotation marks with its capture number, then a sentence of "
            "plain English only if the original is genuinely dense. Never smooth out, "
            "modernise or 'clarify' the original phrasing."
        ),
    ),
    "summary": Mode(
        name="Summary", max_tokens=900, quote_first=False,
        persona=(
            "Give the shape of the whole thing: what this material is, how it is "
            "organised, and the handful of points that actually matter. Open with one "
            "sentence that would orient someone who has never seen it. Stay concrete — "
            "name the real sections, numbers and terms rather than describing them "
            "abstractly."
        ),
    ),
    "compare": Mode(
        name="Compare", max_tokens=900,
        persona=(
            "They are asking how things in the brief differ — tiers, versions, options, "
            "screens, or one thing before and after a change. Differences first, "
            "similarities second, and name which capture each side came from. A table is "
            "often clearest. Where the captures cover one side and not the other, say so "
            "instead of half-answering."
        ),
    ),
    "direct": Mode(
        # 180 was a guillotine, not a style: a two-way comparison could not fit, so the
        # answer was cut mid-word and the shorter-answer retry had no room either. The
        # persona is what keeps Direct short; this is only a backstop.
        name="Direct", max_tokens=450, plain=True, quote_first=False,
        persona=(
            "DIRECT MODE — facts only, and SHORT. Override any instruction to be warm or "
            "add personality. The answer from the brief, or \"not in what you showed me\". "
            "No preamble, no hedging, no follow-up questions.\n\n"
            "Four lines at most. If the honest answer has more to it than that — a "
            "comparison with many dimensions, a long list — give the points that matter "
            "most and end with one line saying what else is in there, so they can ask for "
            "it or switch to Compare. Never start something you cannot finish in the "
            "space: a complete short answer is the whole point of this mode."
        ),
    ),
}

DEFAULT_MODE = "answer"


def get_mode(name: str | None) -> Mode:
    return MODES.get((name or DEFAULT_MODE).strip().lower(), MODES[DEFAULT_MODE])


def catalog() -> list[dict]:
    return [{"key": k, "name": m.name, "persona": m.persona.split(".")[0] + "."}
            for k, m in MODES.items()]
