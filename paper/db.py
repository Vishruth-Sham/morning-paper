"""SQLite persistence: profile, plan versions, proposals, editions, runs, notebook."""
from __future__ import annotations

import json
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

DEFAULT_DB = Path(os.environ.get("MORNING_PAPER_DB", Path(__file__).resolve().parent.parent / "data" / "paper.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS profile (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    interests TEXT NOT NULL,
    goals TEXT NOT NULL,
    always_cover TEXT NOT NULL,
    reading_preference TEXT NOT NULL,
    front_page_minutes INTEGER NOT NULL DEFAULT 5,
    business_minutes INTEGER NOT NULL DEFAULT 20,
    timezone TEXT NOT NULL DEFAULT 'UTC',
    preferred_sources TEXT NOT NULL DEFAULT '',
    llm_provider TEXT NOT NULL DEFAULT 'auto',
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS plan_versions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    note TEXT NOT NULL,
    origin TEXT NOT NULL,            -- seed | ai | manual | proposal
    profile_snapshot TEXT NOT NULL,  -- JSON of the profile this plan was built for
    slots TEXT NOT NULL              -- JSON list of slot dicts
);

CREATE TABLE IF NOT EXISTS plan_proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    base_version INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | applied | dismissed
    rationale TEXT NOT NULL DEFAULT '',
    slots TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS editions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    date TEXT NOT NULL,              -- YYYY-MM-DD in the reader's timezone
    issue_no INTEGER NOT NULL,       -- 1 for the day's first issue
    created_at TEXT NOT NULL,
    plan_version INTEGER NOT NULL,
    reading_minutes INTEGER NOT NULL,
    content TEXT NOT NULL,           -- JSON edition document
    UNIQUE (date, issue_no)
);

CREATE TABLE IF NOT EXISTS edition_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    edition_id INTEGER NOT NULL REFERENCES editions(id),
    slot_id TEXT NOT NULL,
    title TEXT NOT NULL,
    publisher TEXT NOT NULL,
    published_at TEXT,
    url TEXT NOT NULL,
    story_key TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_edition_sources_url ON edition_sources(url);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    edition_date TEXT NOT NULL,
    status TEXT NOT NULL,            -- running | published | skipped | failed
    plan_version INTEGER,
    edition_id INTEGER,
    message TEXT NOT NULL DEFAULT '',
    diagnostics TEXT NOT NULL DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    text TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending',  -- pending | answered | dropped
    edition_id INTEGER
);

CREATE TABLE IF NOT EXISTS notebook (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    name TEXT NOT NULL,
    what_it_sells TEXT NOT NULL DEFAULT '',
    revenue_model TEXT NOT NULL DEFAULT '',
    estimated_economics TEXT NOT NULL DEFAULT '',
    acquisition_strategy TEXT NOT NULL DEFAULT '',
    copy_this TEXT NOT NULL DEFAULT '',
    notes TEXT NOT NULL DEFAULT '',
    source_url TEXT NOT NULL DEFAULT '',
    edition_id INTEGER,
    origin TEXT NOT NULL DEFAULT 'manual'  -- manual | edition
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class DB:
    def __init__(self, path: Path | str = DEFAULT_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        conn = sqlite3.connect(self.path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ---------- profile ----------
    def get_profile(self) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM profile WHERE id = 1").fetchone()
        return dict(row) if row else None

    def save_profile(self, p: dict) -> None:
        fields = ["interests", "goals", "always_cover", "reading_preference", "front_page_minutes",
                  "business_minutes", "timezone", "preferred_sources", "llm_provider"]
        vals = [p[f] for f in fields]
        with self.connect() as c:
            c.execute(
                f"INSERT INTO profile (id, {', '.join(fields)}, updated_at) VALUES (1, {', '.join('?' * len(fields))}, ?) "
                f"ON CONFLICT(id) DO UPDATE SET {', '.join(f'{f}=excluded.{f}' for f in fields)}, updated_at=excluded.updated_at",
                (*vals, now_iso()),
            )

    # ---------- plan ----------
    def current_plan(self) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM plan_versions ORDER BY id DESC LIMIT 1").fetchone()
        return _plan(row)

    def get_plan(self, version: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM plan_versions WHERE id = ?", (version,)).fetchone()
        return _plan(row)

    def list_plans(self, limit: int = 20) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT id, created_at, note, origin FROM plan_versions ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(r) for r in rows]

    def save_plan(self, slots: list[dict], note: str, origin: str, profile: dict) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO plan_versions (created_at, note, origin, profile_snapshot, slots) VALUES (?, ?, ?, ?, ?)",
                (now_iso(), note, origin, json.dumps(_profile_core(profile)), json.dumps(slots)),
            )
            return cur.lastrowid

    # ---------- proposals ----------
    def add_proposal(self, base_version: int, slots: list[dict], rationale: str) -> int:
        with self.connect() as c:
            c.execute("UPDATE plan_proposals SET status='dismissed' WHERE status='pending'")
            cur = c.execute(
                "INSERT INTO plan_proposals (created_at, base_version, rationale, slots) VALUES (?, ?, ?, ?)",
                (now_iso(), base_version, rationale, json.dumps(slots)),
            )
            return cur.lastrowid

    def pending_proposal(self) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM plan_proposals WHERE status='pending' ORDER BY id DESC LIMIT 1").fetchone()
        if not row:
            return None
        d = dict(row)
        d["slots"] = json.loads(d["slots"])
        return d

    def set_proposal_status(self, pid: int, status: str) -> None:
        with self.connect() as c:
            c.execute("UPDATE plan_proposals SET status=? WHERE id=?", (status, pid))

    # ---------- editions ----------
    def latest_edition_for(self, date: str) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM editions WHERE date=? ORDER BY issue_no DESC LIMIT 1", (date,)).fetchone()
        return _edition(row)

    def get_edition(self, date: str, issue_no: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM editions WHERE date=? AND issue_no=?", (date, issue_no)).fetchone()
        return _edition(row)

    def get_edition_by_id(self, eid: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM editions WHERE id=?", (eid,)).fetchone()
        return _edition(row)

    def list_editions(self) -> list[dict]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT id, date, issue_no, created_at, plan_version, reading_minutes, content FROM editions ORDER BY date DESC, issue_no DESC"
            ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            content = json.loads(d.pop("content"))
            lead = content.get("lead", {}) or {}
            d["lead_headline"] = lead.get("headline", "")
            also = ""
            for sec in content.get("sections", []):
                if sec.get("kind") == "case_study" and sec.get("items") and sec.get("id") != lead.get("section_id"):
                    it = sec["items"][0]
                    also = "Startup Story: " + (it.get("business_name") or it.get("headline", ""))
                    break
            if not also and lead.get("kind") and lead.get("kind") != "news":
                also = "A quiet news day"
            d["also"] = also
            out.append(d)
        return out

    def publish_edition(self, date: str, content: dict, plan_version: int, reading_minutes: int,
                        sources: list[dict], new_issue: bool = False) -> tuple[int, int] | None:
        """Atomically publish. Returns (edition_id, issue_no), or None if an issue
        already exists for the date and new_issue is False (existing issue stays intact)."""
        with self.connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute("SELECT MAX(issue_no) AS n FROM editions WHERE date=?", (date,)).fetchone()
            existing = row["n"] or 0
            if existing and not new_issue:
                return None
            issue_no = existing + 1
            content = {**content, "date": date, "issue_no": issue_no}
            cur = c.execute(
                "INSERT INTO editions (date, issue_no, created_at, plan_version, reading_minutes, content) VALUES (?, ?, ?, ?, ?, ?)",
                (date, issue_no, now_iso(), plan_version, reading_minutes, json.dumps(content)),
            )
            eid = cur.lastrowid
            for s in sources:
                c.execute(
                    "INSERT INTO edition_sources (edition_id, slot_id, title, publisher, published_at, url, story_key) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (eid, s["slot_id"], s["title"], s["publisher"], s.get("published_at"), s["url"], s.get("story_key", "")),
                )
            return eid, issue_no

    def past_sources(self, days: int = 14) -> list[dict]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT s.*, e.date FROM edition_sources s JOIN editions e ON e.id = s.edition_id "
                "WHERE e.date >= ? ORDER BY e.date DESC",
                ((datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat(),),
            ).fetchall()
        return [dict(r) for r in rows]

    def recent_featured(self, slot_id: str, limit: int = 10) -> list[dict]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT s.*, e.date FROM edition_sources s JOIN editions e ON e.id=s.edition_id WHERE s.slot_id=? ORDER BY e.date DESC, e.issue_no DESC LIMIT ?",
                (slot_id, limit),
            ).fetchall()
        return [dict(r) for r in rows]

    # ---------- runs ----------
    def start_run(self, edition_date: str, plan_version: int) -> int:
        with self.connect() as c:
            cur = c.execute(
                "INSERT INTO runs (started_at, edition_date, status, plan_version) VALUES (?, ?, 'running', ?)",
                (now_iso(), edition_date, plan_version),
            )
            return cur.lastrowid

    def finish_run(self, run_id: int, status: str, message: str, diagnostics: dict, edition_id: int | None = None) -> None:
        with self.connect() as c:
            c.execute(
                "UPDATE runs SET finished_at=?, status=?, message=?, diagnostics=?, edition_id=? WHERE id=?",
                (now_iso(), status, message, json.dumps(diagnostics), edition_id, run_id),
            )

    def list_runs(self, limit: int = 10) -> list[dict]:
        with self.connect() as c:
            rows = c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["diagnostics"] = json.loads(d["diagnostics"] or "{}")
            out.append(d)
        return out

    # ---------- questions for tomorrow ----------
    def add_question(self, text: str) -> int:
        with self.connect() as c:
            return c.execute("INSERT INTO questions (created_at, text) VALUES (?, ?)", (now_iso(), text.strip())).lastrowid

    def list_questions(self, status: str | None = None) -> list[dict]:
        q = "SELECT q.*, e.date AS edition_date, e.issue_no AS edition_issue FROM questions q LEFT JOIN editions e ON e.id=q.edition_id"
        args: tuple = ()
        if status:
            q += " WHERE q.status=?"
            args = (status,)
        with self.connect() as c:
            return [dict(r) for r in c.execute(q + " ORDER BY q.id DESC", args).fetchall()]

    def set_question_status(self, qid: int, status: str, edition_id: int | None = None) -> None:
        with self.connect() as c:
            c.execute("UPDATE questions SET status=?, edition_id=COALESCE(?, edition_id) WHERE id=?", (status, edition_id, qid))

    # ---------- notebook ----------
    NOTE_FIELDS = ["name", "what_it_sells", "revenue_model", "estimated_economics", "acquisition_strategy",
                   "copy_this", "notes", "source_url", "edition_id", "origin"]

    def add_note(self, n: dict) -> int:
        vals = [n.get(f, "" if f not in ("edition_id",) else None) for f in self.NOTE_FIELDS]
        with self.connect() as c:
            cur = c.execute(
                f"INSERT INTO notebook (created_at, updated_at, {', '.join(self.NOTE_FIELDS)}) VALUES (?, ?, {', '.join('?' * len(vals))})",
                (now_iso(), now_iso(), *vals),
            )
            return cur.lastrowid

    def update_note(self, nid: int, n: dict) -> None:
        fields = [f for f in self.NOTE_FIELDS if f in n and f not in ("edition_id", "origin")]
        with self.connect() as c:
            c.execute(
                f"UPDATE notebook SET {', '.join(f'{f}=?' for f in fields)}, updated_at=? WHERE id=?",
                (*[n[f] for f in fields], now_iso(), nid),
            )

    def delete_note(self, nid: int) -> None:
        with self.connect() as c:
            c.execute("DELETE FROM notebook WHERE id=?", (nid,))

    def list_notes(self) -> list[dict]:
        with self.connect() as c:
            rows = c.execute(
                "SELECT n.*, e.date AS edition_date, e.issue_no AS edition_issue FROM notebook n "
                "LEFT JOIN editions e ON e.id = n.edition_id ORDER BY n.updated_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_note(self, nid: int) -> dict | None:
        with self.connect() as c:
            row = c.execute("SELECT * FROM notebook WHERE id=?", (nid,)).fetchone()
        return dict(row) if row else None


def _profile_core(p: dict) -> dict:
    return {k: p.get(k) for k in ("interests", "goals", "always_cover", "reading_preference",
                                  "front_page_minutes", "business_minutes", "preferred_sources")}


def _plan(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["slots"] = json.loads(d["slots"])
    d["profile_snapshot"] = json.loads(d["profile_snapshot"])
    return d


def _edition(row) -> dict | None:
    if not row:
        return None
    d = dict(row)
    d["content"] = json.loads(d["content"])
    return d
