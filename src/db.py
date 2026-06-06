"""
SQLite state store for the self-recursive-improvement loop.

Schema tracks:
  - proposals: candidate changes (skill patches, memory entries, rubric updates)
  - judge_verdicts: m2.7's per-proposal decision
  - thomas_feedback: user overrides, becomes the actual training signal
  - rubric_versions: judge prompt history so we can eval "did the rubric improve?"
  - sessions_mined: tracking so we don't re-mine the same window
"""

import sqlite3
import json
import time
from contextlib import contextmanager
from pathlib import Path

DB_PATH = Path(__file__).parent.parent / "data" / "loop.db"


SCHEMA = """
CREATE TABLE IF NOT EXISTS proposals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    source_session_id TEXT,
    target_kind TEXT NOT NULL,        -- 'skill_patch' | 'memory_add' | 'skill_create' | 'rubric_update'
    target_path TEXT,                 -- file path for skills/memory
    diff TEXT NOT NULL,               -- the proposed change
    rationale TEXT,                   -- why m3 thinks this change should happen
    evidence TEXT,                    -- session excerpts / signals
    confidence REAL DEFAULT 0.5,      -- proposer's self-score
    status TEXT DEFAULT 'pending',    -- pending | approved | rejected | overridden | merged
    rubric_version INTEGER,
    merged_at REAL
);

CREATE TABLE IF NOT EXISTS judge_verdicts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    judged_at REAL NOT NULL,
    judge_model TEXT NOT NULL,
    rubric_version INTEGER NOT NULL,
    verdict TEXT NOT NULL,            -- 'approve' | 'reject' | 'needs_work'
    score REAL,                       -- 0.0 - 1.0
    reasoning TEXT,
    FOREIGN KEY (proposal_id) REFERENCES proposals(id)
);

CREATE TABLE IF NOT EXISTS thomas_feedback (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    proposal_id INTEGER NOT NULL,
    feedback_at REAL NOT NULL,
    verdict TEXT NOT NULL,            -- 'approve' | 'reject' | 'modify'
    note TEXT,                        -- free text from thomas
    was_overrides_judge BOOLEAN NOT NULL,  -- did this disagree with m2.7?
    FOREIGN KEY (proposal_id) REFERENCES proposals(id)
);

CREATE TABLE IF NOT EXISTS rubric_versions (
    version INTEGER PRIMARY KEY,
    created_at REAL NOT NULL,
    prompt_text TEXT NOT NULL,
    approval_rate REAL,               -- m2.7 approved N, thomas overrode M
    override_rate REAL,               -- M / N — the metric we minimize
    parent_version INTEGER,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS sessions_mined (
    session_id TEXT PRIMARY KEY,
    mined_at REAL NOT NULL,
    proposals_generated INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS lessons_learned (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    category TEXT NOT NULL,           -- 'pattern' | 'gap' | 'lesson' | 'correction'
    content TEXT NOT NULL,
    source TEXT,                      -- which session / proposal
    sent_in_digest_at REAL            -- nullable; weekly digest marks sent
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_created ON proposals(created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_proposal ON thomas_feedback(proposal_id);
CREATE INDEX IF NOT EXISTS idx_lessons_digest ON lessons_learned(sent_in_digest_at);
"""


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with conn() as c:
        c.executescript(SCHEMA)
        # Seed rubric v1 if empty
        cur = c.execute("SELECT COUNT(*) FROM rubric_versions")
        if cur.fetchone()[0] == 0:
            from rubric import RUBRIC_V1
            c.execute(
                "INSERT INTO rubric_versions (version, created_at, prompt_text, notes) VALUES (1, ?, ?, 'initial seed')",
                (time.time(), RUBRIC_V1),
            )


@contextmanager
def conn():
    c = sqlite3.connect(DB_PATH)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    try:
        yield c
        c.commit()
    finally:
        c.close()


# --- helpers ---

def add_proposal(target_kind, diff, rationale, evidence,
                 target_path=None, source_session_id=None, confidence=0.5):
    with conn() as c:
        cur = c.execute(
            """INSERT INTO proposals
               (created_at, source_session_id, target_kind, target_path,
                diff, rationale, evidence, confidence, rubric_version)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, (SELECT MAX(version) FROM rubric_versions))""",
            (time.time(), source_session_id, target_kind, target_path,
             diff, rationale, evidence, confidence),
        )
        return cur.lastrowid


def add_judge_verdict(proposal_id, judge_model, verdict, score, reasoning):
    with conn() as c:
        c.execute(
            """INSERT INTO judge_verdicts
               (proposal_id, judged_at, judge_model, rubric_version, verdict, score, reasoning)
               VALUES (?, ?, ?, (SELECT MAX(version) FROM rubric_versions), ?, ?, ?)""",
            (proposal_id, time.time(), judge_model, verdict, score, reasoning),
        )


def add_thomas_feedback(proposal_id, verdict, note=""):
    """Record thomas's decision. Computes whether it overrides the judge."""
    with conn() as c:
        judge = c.execute(
            "SELECT verdict FROM judge_verdicts WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
            (proposal_id,),
        ).fetchone()
        judge_verdict = judge["verdict"] if judge else None
        overrides = judge_verdict is not None and judge_verdict != verdict
        c.execute(
            """INSERT INTO thomas_feedback
               (proposal_id, feedback_at, verdict, note, was_overrides_judge)
               VALUES (?, ?, ?, ?, ?)""",
            (proposal_id, time.time(), verdict, note, overrides),
        )
        # Update proposal status
        new_status = "merged" if verdict == "approve" else "overridden" if overrides else "rejected"
        c.execute("UPDATE proposals SET status = ? WHERE id = ?", (new_status, proposal_id))


def add_lesson(category, content, source=None):
    with conn() as c:
        c.execute(
            "INSERT INTO lessons_learned (created_at, category, content, source) VALUES (?, ?, ?, ?)",
            (time.time(), category, content, source),
        )


def unsent_lessons(limit=20):
    with conn() as c:
        return [dict(r) for r in c.execute(
            "SELECT * FROM lessons_learned WHERE sent_in_digest_at IS NULL ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()]


def mark_lessons_sent(ids):
    with conn() as c:
        c.executemany(
            "UPDATE lessons_learned SET sent_in_digest_at = ? WHERE id = ?",
            [(time.time(), i) for i in ids],
        )


def pending_proposals():
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT p.*, j.verdict AS judge_verdict, j.reasoning AS judge_reasoning, j.score AS judge_score
               FROM proposals p
               LEFT JOIN judge_verdicts j ON j.id = (
                   SELECT id FROM judge_verdicts WHERE proposal_id = p.id ORDER BY id DESC LIMIT 1
               )
               WHERE p.status = 'pending' ORDER BY p.created_at DESC""",
        ).fetchall()]


def override_stats():
    """For rubric eval: how often does thomas override m2.7?"""
    with conn() as c:
        return dict(c.execute(
            """SELECT
                COUNT(*) AS total_judged,
                SUM(CASE WHEN was_overrides_judge THEN 1 ELSE 0 END) AS overrides,
                1.0 * SUM(CASE WHEN was_overrides_judge THEN 1 ELSE 0 END) / COUNT(*) AS override_rate
               FROM thomas_feedback"""
        ).fetchone())


def latest_rubric():
    with conn() as c:
        return dict(c.execute(
            "SELECT * FROM rubric_versions ORDER BY version DESC LIMIT 1"
        ).fetchone())


def save_rubric(prompt_text, parent_version, notes=""):
    with conn() as c:
        c.execute(
            "INSERT INTO rubric_versions (version, created_at, prompt_text, parent_version, notes) VALUES ((SELECT MAX(version)+1 FROM rubric_versions), ?, ?, ?, ?)",
            (time.time(), prompt_text, parent_version, notes),
        )


def was_session_mined(session_id):
    with conn() as c:
        return c.execute(
            "SELECT 1 FROM sessions_mined WHERE session_id = ?", (session_id,)
        ).fetchone() is not None


def mark_session_mined(session_id, proposals=0):
    with conn() as c:
        c.execute(
            "INSERT OR REPLACE INTO sessions_mined (session_id, mined_at, proposals_generated) VALUES (?, ?, ?)",
            (session_id, time.time(), proposals),
        )
