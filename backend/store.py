"""SQLite store: subjects, the captures banked against them, and the brief.

The shape follows the product exactly. A SUBJECT is one thing you're studying. Against
it sit SHOTS, in the order you showed them — each one is the notes a model took from a
single capture, because the frame itself is never kept. Compacting those notes produces
the BRIEF, and the brief is the entire world an answer is allowed to draw on.

Frames are deliberately not stored. A brief compacted from text is cheap to rebuild, has
no image-count ceiling, and lets the fortieth shot of a long scroll cost what the first
one did. It also means the brain that syncs to GitHub stays small enough to push.

Thread-safe: FastAPI serves sync endpoints on a threadpool.
"""
from __future__ import annotations

import sqlite3
import threading
import time
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS subjects (
    id        INTEGER PRIMARY KEY,
    name      TEXT UNIQUE NOT NULL,
    created   REAL NOT NULL,
    last_seen REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS shots (
    id         INTEGER PRIMARY KEY,
    subject_id INTEGER NOT NULL REFERENCES subjects(id),
    seq        INTEGER NOT NULL,
    ts         REAL NOT NULL,
    kind       TEXT NOT NULL DEFAULT 'screen',   -- 'screen' | 'url'
    source     TEXT,                             -- the URL, for kind='url'
    label      TEXT,                             -- optional caption you typed
    summary    TEXT NOT NULL DEFAULT '',         -- one line, for the shot list
    note       TEXT NOT NULL,                    -- the full reading; the only record
    edited     REAL                              -- when a human corrected it, if ever
);
CREATE INDEX IF NOT EXISTS idx_shots_subject_seq ON shots(subject_id, seq);
CREATE TABLE IF NOT EXISTS briefs (
    subject_id INTEGER PRIMARY KEY REFERENCES subjects(id),
    text       TEXT NOT NULL,
    updated    REAL NOT NULL,
    n_shots    INTEGER NOT NULL DEFAULT 0,
    edited     REAL                              -- when a human corrected it, if ever
);
CREATE TABLE IF NOT EXISTS turns (
    id         INTEGER PRIMARY KEY,
    subject_id INTEGER REFERENCES subjects(id),
    ts         REAL NOT NULL,
    question   TEXT NOT NULL,
    answer     TEXT
);
CREATE INDEX IF NOT EXISTS idx_turns_subject_ts ON turns(subject_id, ts);
CREATE TABLE IF NOT EXISTS usage (
    id                INTEGER PRIMARY KEY,
    ts                REAL NOT NULL,
    kind              TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    cost              REAL NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


class Store:
    def __init__(self, db_path: str = "webdata/study.db") -> None:
        path = Path(db_path).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            self._migrate()
            self._conn.commit()

    def _migrate(self) -> None:
        """Add columns to a database that already exists. Call with _lock held.

        The brain in the repo predates these, and it is not disposable — it is the only
        copy of what someone captured. CREATE TABLE IF NOT EXISTS silently leaves an old
        table alone, so new columns need adding by hand.
        """
        for table, column, decl in (("shots", "edited", "REAL"),
                                    ("briefs", "edited", "REAL")):
            have = {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}
            if column not in have:
                self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # --- subjects -----------------------------------------------------------------
    @staticmethod
    def _norm(name: str) -> str:
        return "".join(c for c in (name or "").lower() if c.isalnum())

    def _sid(self, name: str) -> int | None:
        """Resolve a loosely-typed name to a subject id. Exact first, then a
        punctuation- and space-insensitive match, so "acme pricing" finds "Acme Pricing"."""
        row = self._conn.execute("SELECT id FROM subjects WHERE name = ?", (name,)).fetchone()
        if row:
            return row["id"]
        target = self._norm(name)
        if not target:
            return None
        for r in self._conn.execute("SELECT id, name FROM subjects").fetchall():
            if self._norm(r["name"]) == target:
                return r["id"]
        return None

    def _get_or_create(self, name: str, now: float) -> int:
        sid = self._sid(name)
        if sid is not None:
            self._conn.execute("UPDATE subjects SET last_seen = ? WHERE id = ?", (now, sid))
            return sid
        cur = self._conn.execute(
            "INSERT INTO subjects (name, created, last_seen) VALUES (?, ?, ?)", (name, now, now))
        return cur.lastrowid

    def ensure_subject(self, name: str, now: float | None = None) -> str:
        now = time.time() if now is None else now
        name = (name or "").strip()
        if not name:
            raise ValueError("A subject needs a name.")
        with self._lock:
            sid = self._get_or_create(name, now)
            row = self._conn.execute("SELECT name FROM subjects WHERE id = ?", (sid,)).fetchone()
            self._conn.commit()
        return row["name"]

    def subjects(self) -> list[dict]:
        """Everything studied, newest first — the library."""
        with self._lock:
            rows = self._conn.execute(
                "SELECT s.id, s.name, s.created, s.last_seen,"
                " (SELECT COUNT(*) FROM shots x WHERE x.subject_id = s.id) AS n_shots,"
                " (SELECT COUNT(*) FROM turns t WHERE t.subject_id = s.id) AS n_turns,"
                " (SELECT b.updated FROM briefs b WHERE b.subject_id = s.id) AS sealed"
                " FROM subjects s ORDER BY s.last_seen DESC").fetchall()
        return [dict(r) for r in rows]

    def rename_subject(self, old: str, new: str) -> str | None:
        new = (new or "").strip()
        if not new:
            return None
        with self._lock:
            sid = self._sid(old)
            if sid is None or (self._sid(new) not in (None, sid)):
                return None
            self._conn.execute("UPDATE subjects SET name = ? WHERE id = ?", (new, sid))
            self._conn.commit()
        return new

    def delete_subject(self, name: str) -> bool:
        with self._lock:
            sid = self._sid(name)
            if sid is None:
                return False
            for tbl in ("shots", "briefs", "turns"):
                self._conn.execute(f"DELETE FROM {tbl} WHERE subject_id = ?", (sid,))
            self._conn.execute("DELETE FROM subjects WHERE id = ?", (sid,))
            self._conn.commit()
        return True

    # --- shots --------------------------------------------------------------------
    def add_shot(self, subject: str, note: str, summary: str = "", label: str = "",
                 kind: str = "screen", source: str = "", now: float | None = None) -> int:
        """File one capture's reading. Returns its 1-based position in the series."""
        now = time.time() if now is None else now
        with self._lock:
            sid = self._get_or_create(subject, now)
            row = self._conn.execute(
                "SELECT COALESCE(MAX(seq), 0) AS m FROM shots WHERE subject_id = ?",
                (sid,)).fetchone()
            seq = int(row["m"]) + 1
            self._conn.execute(
                "INSERT INTO shots (subject_id, seq, ts, kind, source, label, summary, note)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (sid, seq, now, kind, (source or "").strip() or None,
                 (label or "").strip() or None, (summary or "").strip(), note))
            self._conn.commit()
        return seq

    def shots(self, subject: str) -> list[dict]:
        """Every capture's reading, in the order it was shown."""
        with self._lock:
            sid = self._sid(subject)
            if sid is None:
                return []
            rows = self._conn.execute(
                "SELECT id, seq, ts, kind, source, label, summary, note, edited FROM shots"
                " WHERE subject_id = ? ORDER BY seq", (sid,)).fetchall()
        return [dict(r) for r in rows]

    def last_shot(self, subject: str) -> dict | None:
        """The previous capture — handed to the reader so it can tell a scrolled
        continuation of one page from a genuinely new screen."""
        rows = self.shots(subject)
        return rows[-1] if rows else None

    def shot_count(self, subject: str) -> int:
        with self._lock:
            sid = self._sid(subject)
            if sid is None:
                return 0
            row = self._conn.execute(
                "SELECT COUNT(*) AS c FROM shots WHERE subject_id = ?", (sid,)).fetchone()
        return row["c"] if row else 0

    def update_shot(self, shot_id: int, note: str | None = None,
                    summary: str | None = None, now: float | None = None) -> dict | None:
        """Correct a capture's record by hand.

        This is not a hole in the closed world — it is the closed world working. The
        person typing was the one looking at the screen, so they are a source the reader
        never had: when the note says a label was too small to read, they can simply read
        it. What matters is that the correction is VISIBLE, which is why it is stamped.
        """
        now = time.time() if now is None else now
        sets, args = [], []
        if note is not None and note.strip():
            sets.append("note = ?"); args.append(note.strip())
        if summary is not None:
            sets.append("summary = ?"); args.append(summary.strip()[:200])
        if not sets:
            return None
        sets.append("edited = ?"); args.append(now)
        args.append(int(shot_id))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE shots SET {', '.join(sets)} WHERE id = ?", args)
            if not cur.rowcount:
                return None
            row = self._conn.execute(
                "SELECT id, seq, kind, source, label, summary, note, edited FROM shots"
                " WHERE id = ?", (int(shot_id),)).fetchone()
            self._conn.commit()
        return dict(row) if row else None

    def update_brief(self, subject: str, text: str, now: float | None = None) -> bool:
        """Edit the brief in place. Survives a re-seal because compaction is handed the
        existing brief and told to carry forward what the new captures don't change."""
        now = time.time() if now is None else now
        with self._lock:
            sid = self._sid(subject)
            if sid is None:
                return False
            cur = self._conn.execute(
                "UPDATE briefs SET text = ?, updated = ?, edited = ? WHERE subject_id = ?",
                (text.strip(), now, now, sid))
            self._conn.commit()
        return cur.rowcount > 0

    def drop_shot(self, shot_id: int) -> bool:
        """Bin one capture (a mis-fire, a stray desktop). Sequence numbers are NOT
        renumbered: they record the order things were shown, not an array index."""
        with self._lock:
            cur = self._conn.execute("DELETE FROM shots WHERE id = ?", (int(shot_id),))
            self._conn.commit()
        return cur.rowcount > 0

    def clear_shots(self, subject: str) -> int:
        with self._lock:
            sid = self._sid(subject)
            if sid is None:
                return 0
            cur = self._conn.execute("DELETE FROM shots WHERE subject_id = ?", (sid,))
            self._conn.commit()
        return cur.rowcount

    # --- the brief ----------------------------------------------------------------
    def get_brief(self, subject: str) -> dict | None:
        with self._lock:
            sid = self._sid(subject)
            if sid is None:
                return None
            row = self._conn.execute(
                "SELECT text, updated, n_shots, edited FROM briefs WHERE subject_id = ?",
                (sid,)).fetchone()
        return dict(row) if row else None

    def save_brief(self, subject: str, text: str, n_shots: int,
                   now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            sid = self._get_or_create(subject, now)
            self._conn.execute(
                "INSERT INTO briefs (subject_id, text, updated, n_shots) VALUES (?, ?, ?, ?)"
                " ON CONFLICT(subject_id) DO UPDATE SET"
                " text = excluded.text, updated = excluded.updated, n_shots = excluded.n_shots",
                (sid, text, now, int(n_shots)))
            self._conn.commit()

    def clear_brief(self, subject: str) -> bool:
        with self._lock:
            sid = self._sid(subject)
            if sid is None:
                return False
            cur = self._conn.execute("DELETE FROM briefs WHERE subject_id = ?", (sid,))
            self._conn.commit()
        return cur.rowcount > 0

    # --- the conversation about a subject -----------------------------------------
    def add_turn(self, subject: str, question: str, answer: str,
                 now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            sid = self._get_or_create(subject, now)
            self._conn.execute(
                "INSERT INTO turns (subject_id, ts, question, answer) VALUES (?, ?, ?, ?)",
                (sid, now, question, answer))
            self._conn.commit()

    def turns(self, subject: str, limit: int = 200) -> list[dict]:
        with self._lock:
            sid = self._sid(subject)
            if sid is None:
                return []
            rows = self._conn.execute(
                "SELECT ts, question, answer FROM turns WHERE subject_id = ?"
                " ORDER BY ts DESC LIMIT ?", (sid, limit)).fetchall()
        return [dict(r) for r in reversed(rows)]

    def clear_turns(self, subject: str) -> int:
        with self._lock:
            sid = self._sid(subject)
            if sid is None:
                return 0
            cur = self._conn.execute("DELETE FROM turns WHERE subject_id = ?", (sid,))
            self._conn.commit()
        return cur.rowcount

    # --- state --------------------------------------------------------------------
    def get_state(self, key: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT value FROM state WHERE key = ?", (key,)).fetchone()
        return row["value"] if row else None

    def set_state(self, key: str, value: str) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO state (key, value) VALUES (?, ?)"
                " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, str(value)))
            self._conn.commit()

    def current_subject(self) -> str | None:
        name = self.get_state("current_subject")
        if not name:
            return None
        with self._lock:
            sid = self._sid(name)
            row = (self._conn.execute("SELECT name FROM subjects WHERE id = ?", (sid,)).fetchone()
                   if sid is not None else None)
        return row["name"] if row else None

    def set_current_subject(self, name: str) -> str:
        canonical = self.ensure_subject(name)
        self.set_state("current_subject", canonical)
        return canonical

    # --- usage --------------------------------------------------------------------
    def log_usage(self, kind: str, model: str, prompt_tokens: int, completion_tokens: int,
                  cost: float = 0.0, now: float | None = None) -> None:
        now = time.time() if now is None else now
        with self._lock:
            self._conn.execute(
                "INSERT INTO usage (ts, kind, model, prompt_tokens, completion_tokens, cost)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (now, kind, model, int(prompt_tokens), int(completion_tokens), float(cost)))
            self._conn.commit()

    def usage_summary(self, now: float | None = None) -> dict:
        now = time.time() if now is None else now
        midnight = time.mktime(time.localtime(now)[:3] + (0, 0, 0, 0, 0, -1))

        def totals(where: str, params: tuple) -> dict:
            row = self._conn.execute(
                "SELECT COALESCE(SUM(prompt_tokens), 0) AS pt,"
                " COALESCE(SUM(completion_tokens), 0) AS ct,"
                " COALESCE(SUM(cost), 0) AS cost, COUNT(*) AS calls"
                f" FROM usage {where}", params).fetchone()
            return {"prompt_tokens": row["pt"], "completion_tokens": row["ct"],
                    "cost": round(row["cost"], 4), "calls": row["calls"]}

        with self._lock:
            return {"today": totals("WHERE ts >= ?", (midnight,)), "total": totals("", ())}
