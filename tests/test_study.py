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
import re

import pytest

from backend import fetch, files, prompts
from backend.store import Store
from backend.study import NoBrief, Study, _split_summary
from tests.fakes import FakeLLM

IMG = b"\xff\xd8\xff\xe0fake-jpeg"
FIXTURES = pathlib.Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def text_pdf() -> bytes:
    return (FIXTURES / "text.pdf").read_bytes()


@pytest.fixture(scope="session")
def scan_pdf() -> bytes:
    return (FIXTURES / "scan.pdf").read_bytes()
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


# ============ uploaded files ============
def test_a_text_file_needs_no_model_call_to_be_read():
    """The rule the whole module turns on: if the bytes already carry words, lift them.
    Reading a picture of the same words costs a call and can misread a digit."""
    ex = files.extract("notes.md", b"# Heading\n\nTier A costs $10 per month.\n")
    assert ex.kind == "text" and len(ex.pieces) == 1
    assert ex.pieces[0].is_text and "$10" in ex.pieces[0].text


def test_an_image_is_banked_for_the_vision_reader():
    png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    ex = files.extract("shot.png", png)
    assert ex.kind == "image" and ex.pieces[0].image == png
    assert not ex.pieces[0].is_text


def test_bytes_beat_the_extension():
    """A PDF saved as .txt is still a PDF, and a screenshot named .pdf is still an image."""
    assert files._sniff("report.txt", b"%PDF-1.4 rest") == "pdf"
    assert files._sniff("scan.pdf", b"\x89PNG\r\n\x1a\n" + b"\x00" * 20) == "image"


def test_json_is_pretty_printed_so_its_structure_survives_reading():
    ex = files.extract("cfg.json", b'{"tier":"team","seats":10}')
    assert "\n" in ex.pieces[0].text, "one long line reads far worse than indented JSON"
    assert '"seats": 10' in ex.pieces[0].text


def test_an_unreadable_type_says_what_it_can_take():
    with pytest.raises(files.FileError) as e:
        files.extract("clip.mov", b"\x00\x00\x00\x20ftypqt  " + b"\x00" * 40)
    assert "PDF" in str(e.value) and "capture" in str(e.value)


def test_an_oversized_file_is_refused_with_the_limit_and_a_way_forward():
    with pytest.raises(files.FileError) as e:
        files.extract("huge.pdf", b"%PDF-" + b"x" * files.MAX_UPLOAD_BYTES)
    assert "MB" in str(e.value) and "section" in str(e.value)


def test_an_empty_file_is_refused():
    with pytest.raises(files.FileError):
        files.extract("nothing.txt", b"")


def test_a_pdf_with_a_text_layer_becomes_one_text_capture_per_page(text_pdf):
    """Page order is load-bearing — it is what lets the reader treat page 4 as following
    page 3 rather than as a new document."""
    ex = files.extract("plans.pdf", text_pdf)
    assert ex.kind == "pdf"
    assert [p.label for p in ex.pieces] == ["page 1", "page 2"]
    assert all(p.is_text for p in ex.pieces), "a text layer must never cost a model call"
    assert "Starter $9" in ex.pieces[0].text
    assert "Overage" in ex.pieces[1].text


def test_a_scanned_pdf_falls_back_to_the_page_image_and_says_it_did(scan_pdf):
    ex = files.extract("scan.pdf", scan_pdf)
    assert len(ex.pieces) == 1 and not ex.pieces[0].is_text
    assert ex.pieces[0].image[:2] in (b"\xff\xd8", b"\x89P"), "a real image blob"
    assert any("no text layer" in n for n in ex.notes), "the extra cost must be visible"


def test_a_long_pdf_is_capped_and_says_so(text_pdf):
    ex = files.extract("plans.pdf", text_pdf, max_pages=1)
    assert len(ex.pieces) == 1
    assert any("only the first 1" in n for n in ex.notes)


def test_uploaded_pages_bank_through_the_ordinary_capture_path(text_pdf):
    """However a page arrives, it lands in the same shot list, gets a sequence number and
    can be binned — the closed world stays auditable."""
    study, store, llm = build()
    ex = files.extract("plans.pdf", text_pdf)
    for piece in ex.pieces:
        study.capture_text("Nimbus", title=f"plans.pdf — {piece.label}",
                           text=piece.text, source="plans.pdf")
    shots = store.shots("Nimbus")
    assert [s["seq"] for s in shots] == [1, 2]
    assert not llm.of_kind("read"), "text pages cost nothing to take in"
    study.seal("Nimbus")
    assert "Starter $9" in llm.of_kind("brief")[0]["user"]


def test_reasoning_over_the_material_is_in_scope():
    """Asked how two things differ, it listed each and left him to compare them himself,
    opening with "the brief doesn't directly compare them". Reading the gate that way
    makes it a worse reader, not a safer one: the difference was derivable from what he
    had captured, and stating it is reporting, not importing."""
    assert "THE RESTRICTION IS ON WHERE FACTS COME FROM" in prompts.CLOSED_WORLD
    assert "work the difference out and TELL them" in prompts.CLOSED_WORLD
    # ...while the actual restriction is untouched
    assert "may never do is reach outside the brief" in prompts.CLOSED_WORLD


def test_direct_mode_can_hold_a_complete_short_answer():
    """180 tokens could not fit a two-way comparison, so the reply was cut mid-word and
    the shorter-answer retry had no room either. Terseness is the persona's job."""
    from backend.modes import MODES
    assert MODES["direct"].max_tokens >= 400
    assert "facts only" in MODES["direct"].persona.lower()


def test_a_caveat_in_the_brief_is_not_treated_as_an_absence():
    """Asked for a board's pinout, it answered "only the heading was captured" — about a
    brief that held the full pin-by-pin listing. The reader had honestly noted that a
    heading was clipped and one chip label was too small to read; those caveats about
    individual details got generalised into the whole section being missing. Refusing on
    top of real data is worse than any wrong answer: it hides what they did capture, and
    they have no way to know."""
    assert "BEFORE YOU SAY THEY DIDN'T SHOW YOU SOMETHING, LOOK" in prompts.CLOSED_WORLD
    assert "A caveat is not an absence" in prompts.CLOSED_WORLD
    assert "GIVE what is there and name only the part" in prompts.CLOSED_WORLD


# ============ correcting what was recorded ============
def test_correcting_a_capture_stamps_it_so_the_brief_stays_auditable():
    """Editing is not a hole in the closed world — the person typing was the one looking
    at the screen, and knows what the reader could not read. What matters is that the
    correction is visible afterwards."""
    study, store, _ = build()
    study.capture("Acme pricing", IMG)
    shot = store.shots("Acme pricing")[0]
    assert shot["edited"] is None
    out = store.update_shot(shot["id"], note="Chip is ESP32-FH4R2. Serial 88-A17.")
    assert "ESP32-FH4R2" in out["note"]
    assert store.shots("Acme pricing")[0]["edited"] is not None


def test_a_corrected_capture_is_what_the_next_seal_compacts():
    """The note is the source the brief is rebuilt from, so correcting it is the durable
    fix — anything else gets overwritten the next time Done is pressed."""
    study, store, llm = build()
    study.capture("Acme pricing", IMG)
    store.update_shot(store.shots("Acme pricing")[0]["id"],
                      note="The label reads ESP32-FH4R2, legible in person.")
    study.seal("Acme pricing")
    assert "ESP32-FH4R2" in llm.of_kind("brief")[0]["user"]


def test_an_edited_brief_survives_the_next_seal():
    """The trap this feature could have shipped with: edit the brief, add a capture, press
    Done, and watch the edit vanish. Compaction is handed the existing brief and told to
    carry forward what the new captures don't change, so the edit goes in as source."""
    study, store, llm = sealed()
    store.update_brief("Acme pricing", "# Acme\nHand-corrected: the annual discount is 18%.")
    study.capture("Acme pricing", IMG)
    study.seal("Acme pricing")
    merge = llm.of_kind("brief")[-1]
    assert "annual discount is 18%" in merge["user"], "the edit must reach compaction"
    assert "EXISTING BRIEF" in merge["user"]
    assert "AN EARLIER BRIEF" in merge["full_system"]


def test_an_edited_brief_is_what_answers_come_from():
    study, store, llm = sealed()
    store.update_brief("Acme pricing", "# Acme\nThe only fact: support closes at 5pm.")
    study.ask("when does support close?", "Acme pricing")
    assert "support closes at 5pm" in llm.of_kind("ask")[-1]["full_system"]
    assert "Everything, compacted." not in llm.of_kind("ask")[-1]["full_system"]


def test_editing_refuses_an_empty_or_missing_target():
    study, store, _ = build()
    study.capture("Acme pricing", IMG)
    shot = store.shots("Acme pricing")[0]
    assert store.update_shot(shot["id"]) is None, "nothing to change is not an edit"
    assert store.update_shot(999_999, note="whatever") is None
    assert store.update_brief("Never Sealed", "some text") is False


def test_an_older_brain_gains_the_edited_columns():
    """The brain in the repo predates these columns and is the only copy of what someone
    captured. CREATE TABLE IF NOT EXISTS would have left it silently without them."""
    import sqlite3, tempfile, pathlib
    tmp = pathlib.Path(tempfile.mkdtemp()) / "old.db"
    old = sqlite3.connect(tmp)
    old.executescript("""
        CREATE TABLE subjects (id INTEGER PRIMARY KEY, name TEXT UNIQUE NOT NULL,
                               created REAL NOT NULL, last_seen REAL NOT NULL);
        CREATE TABLE shots (id INTEGER PRIMARY KEY, subject_id INTEGER NOT NULL,
                            seq INTEGER NOT NULL, ts REAL NOT NULL,
                            kind TEXT NOT NULL DEFAULT 'screen', source TEXT, label TEXT,
                            summary TEXT NOT NULL DEFAULT '', note TEXT NOT NULL);
        CREATE TABLE briefs (subject_id INTEGER PRIMARY KEY, text TEXT NOT NULL,
                             updated REAL NOT NULL, n_shots INTEGER NOT NULL DEFAULT 0);
    """)
    old.execute("INSERT INTO subjects VALUES (1,'Kept',0,0)")
    old.execute("INSERT INTO shots (subject_id,seq,ts,note) VALUES (1,1,0,'the old note')")
    old.commit(); old.close()

    store = Store(str(tmp))
    assert "edited" in {r[1] for r in store._conn.execute("PRAGMA table_info(shots)")}
    assert "edited" in {r[1] for r in store._conn.execute("PRAGMA table_info(briefs)")}
    assert store.shots("Kept")[0]["note"] == "the old note", "existing data must survive"


def test_health_reports_the_deployed_commit(monkeypatch):
    """"Is my fix live?" has to be answerable from outside without the password. Without
    this, the only signal was a process-start timestamp — which answers when the container
    booted, not what it is running, and working one out from the other by hand is exactly
    the kind of silent guess this app exists to avoid."""
    from backend import main
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "9398904abcdef1234567")
    assert main.commit_sha() == "9398904abcde"
    monkeypatch.delenv("RAILWAY_GIT_COMMIT_SHA")
    monkeypatch.setenv("SOURCE_COMMIT", "deadbeefcafe0000")
    assert main.commit_sha() == "deadbeefcafe"
    monkeypatch.delenv("SOURCE_COMMIT")
    assert main.commit_sha() == "", "absent is empty, never a fabricated value"


# ============ YouTube ============
@pytest.mark.parametrize("url", [
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?t=42",
    "https://m.youtube.com/watch?si=trackingjunk&v=dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ",
])
def test_the_video_id_survives_real_share_urls(url):
    """Nobody pastes the canonical form. Mobile links put tracking params before `v=`."""
    assert fetch.youtube_id(url) == "dQw4w9WgXcQ"


@pytest.mark.parametrize("url", [
    "https://docs.blender.org/manual/",
    "https://vimeo.com/12345678",
    "not a url at all",
])
def test_a_non_youtube_link_is_left_to_the_page_fetcher(url):
    assert fetch.youtube_id(url) is None


def _fake_snips(seconds: int, every: int = 5):
    return [(float(t), f"word at {t}") for t in range(0, seconds, every)]


def test_a_short_video_stays_one_capture(monkeypatch):
    monkeypatch.setattr(fetch, "_snippets", lambda vid: _fake_snips(300))
    monkeypatch.setattr(fetch, "_youtube_title", lambda vid: "A Short Talk — Someone")
    title, segments = fetch.youtube("https://youtu.be/dQw4w9WgXcQ")
    assert title == "A Short Talk — Someone"
    assert len(segments) == 1 and segments[0][0] == "transcript"


def test_a_long_video_becomes_several_captures_labelled_by_time(monkeypatch):
    """The same treatment a PDF gets. The shot list stays navigable, and a section you
    don't care about can be binned without re-fetching the rest."""
    monkeypatch.setattr(fetch, "_snippets", lambda vid: _fake_snips(3600))
    monkeypatch.setattr(fetch, "_youtube_title", lambda vid: "A Long Talk")
    _, segments = fetch.youtube("https://youtu.be/dQw4w9WgXcQ")
    assert len(segments) == 6, "an hour at ten minutes a segment"
    assert segments[0][0].startswith("0:00–")
    assert "–" in segments[-1][0]
    joined = " ".join(s[1] for s in segments)
    assert "word at 0" in joined and "word at 3595" in joined, "nothing dropped at a seam"


def test_timestamps_are_kept_so_a_quote_can_be_found_again(monkeypatch):
    """"Where does it say that?" is a question people actually ask of a video, and "at
    12:40" is worth far more than the same sentence with no way back to it."""
    monkeypatch.setattr(fetch, "_snippets", lambda vid: _fake_snips(120))
    monkeypatch.setattr(fetch, "_youtube_title", lambda vid: "T")
    _, segments = fetch.youtube("https://youtu.be/dQw4w9WgXcQ")
    text = segments[0][1]
    assert text.startswith("[0:00]")
    assert "[1:00]" in text
    assert text.count("[") < 25, "a stamp every 30s, not on every caption line"


def test_an_hour_long_video_stamps_with_hours(monkeypatch):
    monkeypatch.setattr(fetch, "_snippets", lambda vid: _fake_snips(7200))
    monkeypatch.setattr(fetch, "_youtube_title", lambda vid: "T")
    _, segments = fetch.youtube("https://youtu.be/dQw4w9WgXcQ")
    assert any("1:00:00" in s[1] for s in segments), "past an hour, h:mm:ss"


def test_no_captions_says_what_to_do_instead(monkeypatch):
    def boom(vid):
        raise fetch.FetchError(
            "Couldn't get a transcript for that video — captions may be turned off, or "
            "YouTube blocked the request. If it's your own recording, paste the text "
            "instead; otherwise capture the video's page from your screen.")
    monkeypatch.setattr(fetch, "_snippets", boom)
    with pytest.raises(fetch.FetchError) as e:
        fetch.youtube("https://youtu.be/dQw4w9WgXcQ")
    assert "capture the video's page" in str(e.value)


def test_compaction_is_told_to_keep_locators():
    """47,000 characters of timestamped transcript compacted to a 12,600-character brief
    with ZERO timestamps left in it — so "where in the video does he say that?" had no
    answer. Locators are the only way back to the source, and they cost almost nothing
    to keep."""
    assert "KEEP THE LOCATORS" in prompts.BRIEF
    assert "timestamp like" in prompts.BRIEF
    assert "stitching is about removing repetition, not about removing the map" in prompts.BRIEF


def test_compaction_is_told_it_has_room():
    """That brief used 39% of the tokens it was allowed. It compressed because nothing
    told it not to, not because it ran out of space."""
    assert "USE THE ROOM YOU HAVE" in prompts.BRIEF
    assert "a short brief is a lossy one" in prompts.BRIEF


# --- the frontend contract -----------------------------------------------------------
# app.js reaches into the page by id and nothing checks that the id is there. A missing
# one throws on a null at boot and the app is simply blank — no error anyone will read,
# and nothing a backend test would notice. These two are cheap and catch the whole class.

FRONTEND = pathlib.Path(__file__).resolve().parents[1] / "frontend"


def _element_ids(html: str) -> set[str]:
    return set(re.findall(r'\bid="([^"]+)"', html))


def test_every_id_the_script_reaches_for_exists_in_the_page():
    js = (FRONTEND / "app.js").read_text()
    html = (FRONTEND / "index.html").read_text()
    wanted = set(re.findall(r'\$\("([^"]+)"\)', js))
    assert wanted, "the $() helper moved — this test is now checking nothing"
    missing = sorted(wanted - _element_ids(html))
    assert not missing, f"app.js reads ids that index.html doesn't define: {missing}"


def test_the_mode_rail_still_has_a_select_behind_it():
    """The sigils are a skin. The <select> is what actually holds the mode and what the
    change listener hangs off, so a redesign that deletes it silently strands the
    picker: it would still light up and still never reach the server."""
    html = (FRONTEND / "index.html").read_text()
    js = (FRONTEND / "app.js").read_text()
    assert re.search(r'<select[^>]*\bid="mode"', html)
    assert 'dispatchEvent(new Event("change"))' in js
