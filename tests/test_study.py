"""Study Buddy's tests.

The first section is the only one that really matters. This buddy makes exactly one
promise — it will not tell you anything you did not show it — and a promise that lives
only in a prompt is one the model can talk itself out of. So the closed world is tested
structurally: the tool does not exist, the context has exactly two sources, and an
answer with no brief behind it is refused rather than improvised.
"""
from __future__ import annotations

import ast
import pathlib

import pytest

from backend import prompts
from backend.store import Store
from backend.study import NoBrief, Study, _split_summary
from tests.fakes import FakeLLM

IMG = b"\xff\xd8\xff\xe0fake-jpeg"
BACKEND = pathlib.Path(__file__).resolve().parent.parent / "backend"


def build(**kw):
    store = Store(":memory:")
    llm = FakeLLM(**kw)
    return Study(llm, store, app_name="Study Buddy"), store, llm


def sealed(**kw):
    """A subject with three captures banked and a brief written."""
    study, store, llm = build(**kw)
    for i in range(3):
        study.capture("Acme pricing", IMG)
    study.seal("Acme pricing")
    return study, store, llm


# ============ the closed world ============
def _code_only(src: str) -> str:
    """The module with comments and docstrings stripped — prose about the closed world
    is fine, a code path through it is not."""
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None)
            if (body and isinstance(body[0], ast.Expr)
                    and isinstance(body[0].value, ast.Constant)
                    and isinstance(body[0].value.value, str)):
                body.pop(0)
    return ast.unparse(tree)          # unparse never re-emits comments


def test_no_search_tool_exists_anywhere_in_the_backend():
    """The guarantee is kept by absence, not by instruction. If a tool were ever wired
    in, a prompt telling the model not to use it would be the only thing standing
    between a user and an answer they cannot audit. String literals still count — that
    is how a tool gets declared."""
    offenders = []
    for path in BACKEND.rglob("*.py"):
        code = _code_only(path.read_text())
        for needle in ("web_search", "tool_use", "tools", "server_tool", "tool_choice"):
            if needle in code:
                offenders.append(f"{path.name}: {needle}")
    assert not offenders, f"a tool path crept into the closed world: {offenders}"


def test_an_answer_draws_on_the_brief_and_the_conversation_and_nothing_else():
    study, store, llm = sealed()
    store.add_turn("Acme pricing", "an earlier question", "an earlier answer")
    study.ask("what does it cost?", "Acme pricing")
    call = llm.of_kind("ask")[-1]
    assert "Everything, compacted." in call["full_system"], "the brief must be in context"
    assert call["history"], "the conversation so far must be in context"
    assert not call["images"], "answering never re-sends images; the notes are the record"


def test_a_question_with_no_brief_is_refused_not_improvised():
    """The tempting failure is answering anyway from general knowledge."""
    study, store, llm = build()
    with pytest.raises(NoBrief):
        study.ask("what does it cost?", "Nothing Captured")
    assert not llm.of_kind("ask"), "no model call may happen without a brief"


def test_captures_banked_but_not_sealed_still_refuse_and_say_why():
    study, store, llm = build()
    study.capture("Acme pricing", IMG)
    with pytest.raises(NoBrief) as e:
        study.ask("what does it cost?", "Acme pricing")
    assert "Done" in str(e.value), "tell them the one thing that would fix it"


def test_every_answering_prompt_carries_the_closed_world_clause():
    study, _, llm = sealed()
    from backend.modes import MODES
    for key in MODES:
        study.set_mode(key)
        study.ask("anything?", "Acme pricing")
        sp = llm.of_kind("ask")[-1]["full_system"]
        assert "ANSWER ONLY FROM WHAT THEY SHOWED YOU" in sp, f"{key} mode lost the gate"
        assert "didn't show me that" in sp


def test_the_gate_distinguishes_a_silent_source_from_an_uncaptured_one():
    """"The page says nothing about it" and "you never showed me that page" are opposite
    facts, and collapsing them is how a closed-world answer misleads."""
    assert "NEVER CAPTURED" in prompts.CLOSED_WORLD
    assert "says nothing" in prompts.CLOSED_WORLD


# ============ capturing ============
def test_each_capture_is_read_once_and_banked_in_order():
    study, store, llm = build()
    for _ in range(3):
        study.capture("Acme pricing", IMG)
    assert len(llm.of_kind("read")) == 3, "one reading per press, not one big batch at the end"
    assert [s["seq"] for s in store.shots("Acme pricing")] == [1, 2, 3]


def test_a_capture_sends_the_image_but_the_brief_is_built_from_text():
    study, store, llm = build()
    study.capture("Acme pricing", IMG)
    assert llm.of_kind("read")[0]["images"] == [IMG]
    study.seal("Acme pricing")
    assert not llm.of_kind("brief")[0]["images"], (
        "compaction is text-in text-out — that is what removes the image-count ceiling "
        "and makes the fortieth shot of a scroll cost what the first one did")


def test_the_reader_is_told_what_the_previous_capture_showed():
    """Without this it cannot tell a scrolled continuation of one page from a new screen,
    which is the whole scroll-and-shoot workflow."""
    study, store, llm = build(read_reply="SUMMARY: pricing table, top half\nDetail.")
    study.capture("Acme pricing", IMG)
    study.capture("Acme pricing", IMG)
    second = llm.of_kind("read")[1]["full_system"]
    assert "pricing table, top half" in second
    assert "continues that one" in second


def test_the_first_capture_carries_no_continuation_hint():
    study, _, llm = build()
    study.capture("Acme pricing", IMG)
    assert "THE CAPTURE BEFORE THIS ONE" not in llm.of_kind("read")[0]["full_system"]


def test_a_subject_names_itself_from_the_first_capture():
    """So the flow starts by showing something, not by filling in a form."""
    study, store, llm = build(name_reply="Acme pricing page")
    out = study.capture(None, IMG)
    assert out["subject"] == "Acme pricing page"
    assert store.current_subject() == "Acme pricing page"


def test_summary_is_split_off_for_the_shot_list():
    summary, body = _split_summary("SUMMARY: a pricing table\nRow one.\nRow two.")
    assert summary == "a pricing table"
    assert "Row one." in body and "SUMMARY:" not in body


def test_a_reading_without_a_summary_line_still_yields_something_listable():
    summary, body = _split_summary("Just some notes with no summary line.")
    assert summary.startswith("Just some notes")
    assert body


def test_a_label_you_typed_reaches_the_reader_and_the_record():
    study, store, llm = build()
    study.capture("Acme pricing", IMG, label="annual tab")
    assert "annual tab" in llm.of_kind("read")[0]["user"]
    assert store.shots("Acme pricing")[0]["label"] == "annual tab"


def test_fetched_page_text_is_banked_without_a_vision_call():
    """A fetched page is better evidence than a screenshot of it, and free to record."""
    study, store, llm = build()
    out = study.capture_text("Acme pricing", "Pricing — Acme", "Tier A costs $10.",
                             source="https://acme.test/pricing")
    assert not llm.of_kind("read"), "no model call is needed to record text"
    shot = store.shots("Acme pricing")[0]
    assert shot["kind"] == "url" and "Tier A costs $10." in shot["note"]
    assert out["n_shots"] == 1


# ============ sealing ============
def test_seal_compacts_every_note_in_the_order_shown():
    study, store, llm = build()
    for i in range(3):
        study.capture("Acme pricing", IMG)
    study.seal("Acme pricing")
    user = llm.of_kind("brief")[0]["user"]
    assert user.index("capture [1]") < user.index("capture [2]") < user.index("capture [3]")
    assert store.get_brief("Acme pricing")["n_shots"] == 3


def test_sealing_again_merges_rather_than_starting_over():
    """Re-reading everything from scratch would be fine; silently dropping what the new
    captures didn't mention would not."""
    study, store, llm = sealed()
    study.capture("Acme pricing", IMG)
    study.seal("Acme pricing")
    second = llm.of_kind("brief")[1]
    assert "EXISTING BRIEF" in second["user"]
    assert "AN EARLIER BRIEF" in second["full_system"]


def test_sealing_nothing_is_an_error_not_an_empty_brief():
    study, _, _ = build()
    with pytest.raises(ValueError):
        study.seal("Never Captured")


# ============ staleness ============
def test_capturing_after_sealing_marks_the_brief_stale():
    study, store, _ = sealed()
    assert not store.get_state("stale:Acme pricing")
    study.capture("Acme pricing", IMG)
    assert store.get_state("stale:Acme pricing")


def test_a_stale_brief_says_so_in_the_prompt_rather_than_answering_as_if_current():
    study, store, llm = sealed()
    study.capture("Acme pricing", IMG)
    study.ask("what does it cost?", "Acme pricing")
    sp = llm.of_kind("ask")[-1]["full_system"]
    assert "NOT in it" in sp and "press Done" in sp


def test_sealing_clears_the_stale_flag():
    study, store, _ = sealed()
    study.capture("Acme pricing", IMG)
    study.seal("Acme pricing")
    assert not store.get_state("stale:Acme pricing")


# ============ the store ============
def test_a_binned_capture_does_not_renumber_the_others():
    """Sequence numbers are the record of what order things were shown in, not an index —
    and the brief cites them."""
    study, store, _ = build()
    for _ in range(3):
        study.capture("Acme pricing", IMG)
    store.drop_shot(store.shots("Acme pricing")[1]["id"])
    assert [s["seq"] for s in store.shots("Acme pricing")] == [1, 3]


def test_a_loosely_typed_subject_still_resolves():
    store = Store(":memory:")
    store.ensure_subject("Acme Pricing Page")
    assert store.shot_count("acme pricing page") == 0    # resolved, not created afresh
    assert len(store.subjects()) == 1


def test_deleting_a_subject_takes_its_captures_and_brief_with_it():
    study, store, _ = sealed()
    assert store.delete_subject("Acme pricing")
    assert store.subjects() == []
    assert store.shots("Acme pricing") == []
    assert store.get_brief("Acme pricing") is None


def test_usage_is_tracked_so_a_long_capture_run_is_not_a_surprise():
    study, store, _ = build()
    study.capture("Acme pricing", IMG)
    assert store.usage_summary()["total"]["calls"] >= 1
    assert store.usage_summary()["total"]["cost"] > 0


# ============ the adapter ============
def test_text_blocks_are_joined_with_a_blank_line_not_welded():
    """Program Buddy shipped "...each item does:Nice — I can read every field..." because
    this joined with "". Same adapter lineage, same bug avoided."""
    from backend.llm import text_of

    class B:
        def __init__(self, t=None):
            if t is not None:
                self.text = t

    class R:
        content = [B("First half:"), B(), B("Second half.")]

    assert text_of(R()) == "First half:\n\nSecond half."


def test_the_brief_rides_in_the_cached_half_of_the_prompt():
    """A session is a long run of questions against one unchanging document — the shape
    caching exists for. The instructions alone are ~500 tokens, under Anthropic's 1024
    floor, so caching them without the brief declares a breakpoint that can never hit."""
    study, _, llm = sealed()
    study.ask("anything?", "Acme pricing")
    call = llm.of_kind("ask")[-1]
    assert "ANSWER ONLY FROM WHAT THEY SHOWED YOU" in call["cached_system"]
    assert "Everything, compacted." in call["cached_system"], "the brief is stable; cache it"
    assert not call["system"], "nothing per-turn to say when the brief is current"


def test_only_per_turn_material_sits_after_the_cache_breakpoint():
    study, store, llm = sealed()
    study.capture("Acme pricing", IMG)          # now stale
    study.ask("anything?", "Acme pricing")
    call = llm.of_kind("ask")[-1]
    assert "NOT in it" in call["system"], "the staleness note changes per turn"
    assert "Everything, compacted." in call["cached_system"]
