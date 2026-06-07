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

-- Negative patterns: written when thomas rejects or overrides the judge.
-- These are the "things to avoid" the proposer should see on the next cycle.
-- Distinct from lessons_learned (which captures positive insights).
CREATE TABLE IF NOT EXISTS negative_patterns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    proposal_id INTEGER NOT NULL,
    target_kind TEXT NOT NULL,
    target_path TEXT,
    reason TEXT NOT NULL,             -- thomas's note (or 'overridden' if blank)
    was_overrides_judge BOOLEAN NOT NULL,
    used_in_prompt_at REAL,           -- last time we injected this in a proposer prompt
    FOREIGN KEY (proposal_id) REFERENCES proposals(id)
);

-- Outcome measurement: every applied change is logged here so we can
-- later grade whether the change actually helped. Outcomes are detected
-- by miner/cycle: reverts in backups, re-corrections of the same target,
-- reappearance of the same lesson, etc.
CREATE TABLE IF NOT EXISTS applied_outcomes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at REAL NOT NULL,
    proposal_id INTEGER NOT NULL,
    target_kind TEXT NOT NULL,
    target_path TEXT NOT NULL,
    diff TEXT NOT NULL,
    applied_at REAL NOT NULL,
    outcome TEXT DEFAULT 'unknown',   -- 'unknown' | 'helped' | 'neutral' | 'reverted' | 'recorrected'
    outcome_detected_at REAL,
    outcome_evidence TEXT,
    FOREIGN KEY (proposal_id) REFERENCES proposals(id)
);

CREATE INDEX IF NOT EXISTS idx_proposals_status ON proposals(status);
CREATE INDEX IF NOT EXISTS idx_proposals_created ON proposals(created_at);
CREATE INDEX IF NOT EXISTS idx_feedback_proposal ON thomas_feedback(proposal_id);
CREATE INDEX IF NOT EXISTS idx_lessons_digest ON lessons_learned(sent_in_digest_at);
CREATE INDEX IF NOT EXISTS idx_neg_patterns_used ON negative_patterns(used_in_prompt_at);
CREATE INDEX IF NOT EXISTS idx_outcomes_target ON applied_outcomes(target_path);
CREATE INDEX IF NOT EXISTS idx_outcomes_outcome ON applied_outcomes(outcome);
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
    """Record thomas's decision. Computes whether it overrides the judge.
    On reject/override, also writes a negative_pattern so the proposer
    can avoid repeating the same class of mistake.
    """
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

        # On reject/override, capture a negative pattern. Skip 'approve'.
        if verdict in ("reject", "modify") or overrides:
            proposal = c.execute(
                "SELECT target_kind, target_path FROM proposals WHERE id = ?",
                (proposal_id,),
            ).fetchone()
            reason = (note or "").strip() or (
                "overridden judge" if overrides else "rejected"
            )
            c.execute(
                """INSERT INTO negative_patterns
                   (created_at, proposal_id, target_kind, target_path,
                    reason, was_overrides_judge)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (time.time(), proposal_id,
                 proposal["target_kind"] if proposal else "unknown",
                 proposal["target_path"] if proposal else None,
                 reason, overrides),
            )


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


# --- negative patterns (avoid-list for the proposer) ---

def recent_negative_patterns(limit: int = 10, min_age_seconds: int = 0) -> list:
    """Return the most recent negative patterns. The proposer injects
    these into its system prompt so it avoids repeating the same class
    of mistake. We skip patterns that were *just* used in a prompt to
    keep the set fresh — caller should call mark_neg_patterns_used()
    after injecting.
    """
    with conn() as c:
        cutoff = time.time() - min_age_seconds
        return [dict(r) for r in c.execute(
            """SELECT * FROM negative_patterns
               WHERE created_at < ?
               ORDER BY created_at DESC LIMIT ?""",
            (cutoff, limit),
        ).fetchall()]


def mark_neg_patterns_used(ids: list):
    with conn() as c:
        c.executemany(
            "UPDATE negative_patterns SET used_in_prompt_at = ? WHERE id = ?",
            [(time.time(), i) for i in ids],
        )


# --- applied outcomes (did the change actually help?) ---

def record_applied_outcome(proposal_id: int, target_kind: str,
                           target_path: str, diff: str) -> int | None:
    """Called by apply.py after a successful write. Returns the row id."""
    with conn() as c:
        cur = c.execute(
            """INSERT INTO applied_outcomes
               (created_at, proposal_id, target_kind, target_path,
                diff, applied_at)
               VALUES (?, ?, ?, ?, ?, ?)""",
            (time.time(), proposal_id, target_kind, target_path,
             diff, time.time()),
        )
        return cur.lastrowid


def unknown_outcomes(limit: int = 50) -> list:
    """Applied changes whose outcome hasn't been graded yet."""
    with conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT * FROM applied_outcomes
               WHERE outcome = 'unknown'
               ORDER BY applied_at ASC LIMIT ?""",
            (limit,),
        ).fetchall()]


def set_outcome(outcome_id: int, outcome: str, evidence: str):
    """outcome in: 'helped' | 'neutral' | 'reverted' | 'recorrected'"""
    assert outcome in ("helped", "neutral", "reverted", "recorrected"), \
        f"bad outcome: {outcome}"
    with conn() as c:
        c.execute(
            """UPDATE applied_outcomes
               SET outcome = ?, outcome_detected_at = ?, outcome_evidence = ?
               WHERE id = ?""",
            (outcome, time.time(), evidence, outcome_id),
        )


def outcome_stats() -> dict:
    """For self_improve / digest: counts of each outcome class."""
    with conn() as c:
        rows = c.execute(
            """SELECT outcome, COUNT(*) AS n
               FROM applied_outcomes
               WHERE outcome != 'unknown'
               GROUP BY outcome"""
        ).fetchall()
    return {r["outcome"]: r["n"] for r in rows}
