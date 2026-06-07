"""
grade_outcomes.py — measure whether applied changes actually helped.

The loop has two feedback signals:
  1. Override rate (thomas vs. judge) — measures *judge calibration*.
  2. Outcome (did the change itself help) — measures *change quality*.

This script grades the second one. Heuristics, in order of cheapness:

  A. Reverted: the file's current content matches a backup more recent
     than the apply timestamp. Strongest "this hurt" signal.
  B. Recorrected: there's a *later* applied change to the same target
     that re-touches the lines we changed. We got out the door but had
     to be fixed. Negative-but-weak.
  C. Lesson re-surfaced: a later lesson/proposal mentions the same
     target_path with conflicting content. Weak negative.
  D. Else: mark 'neutral' after 7 days. Absence of evidence is not
     proof of help, but it's the best signal we have without a real
     eval harness. After ~30 graded outcomes, digest shows a
     helped-vs-not ratio and we can detect drift.

Run nightly. Idempotent — only grades rows still at 'unknown'.
"""

import os
import re
import time
import sqlite3
from pathlib import Path

import db

NEUTRAL_AFTER_SECONDS = 7 * 24 * 3600  # 7 days
REVERT_WINDOW_HOURS = 48  # if a backup newer than apply exists, treat as revert hint


def _backup_modified_after(target: Path, after_ts: float) -> Path | None:
    """Return the newest backup of `target` modified strictly after `after_ts`,
    or None if no such backup exists. Backups live in data/backups/.
    """
    backups_dir = Path(__file__).parent.parent / "data" / "backups"
    if not backups_dir.exists():
        return None
    name = target.name
    candidates = []
    for b in backups_dir.glob(f"*_{name}"):
        # Skip DONE_ markers (those are different files)
        if b.name.startswith("DONE_"):
            continue
        try:
            mt = b.stat().st_mtime
        except OSError:
            continue
        if mt > after_ts:
            candidates.append((mt, b))
    if not candidates:
        return None
    candidates.sort(reverse=True)
    return candidates[0][1]


def _file_matches_backup(target: Path, backup: Path) -> bool:
    """True if target's current content equals the backup's content.
    Means someone (or the apply failure path) restored the file.
    """
    try:
        return target.read_text() == backup.read_text()
    except OSError:
        return False


def _diff_touches_lines(our_diff: str, other_diff: str) -> bool:
    """Heuristic: do these two diffs touch overlapping file regions?
    We just check if either diff contains a chunk of text that also
    appears in the other. Cheap, prone to false positives, but the
    grade is only 'recorrected' (weak negative) so precision matters
    less than recall.
    """
    # Strip diff headers / +/- markers; keep the raw text content.
    def _strip(d: str) -> set:
        out = []
        for line in d.splitlines():
            if line.startswith(("---", "+++", "@@", "diff ", "index ")):
                continue
            tag = line[:1]
            if tag in "+-":
                out.append(line[1:].strip())
            else:
                out.append(line.strip())
        return {l for l in out if len(l) > 12}
    a, b = _strip(our_diff), _strip(other_diff)
    return bool(a & b)


def grade_one(row: dict) -> str | None:
    """Grade a single applied_outcomes row. Returns the new outcome
    string, or None if we can't decide yet (still unknown).
    """
    target = Path(row["target_path"])
    applied_at = row["applied_at"]
    age = time.time() - applied_at

    # (A) Reverted via backup
    backup = _backup_modified_after(target, applied_at)
    if backup and _file_matches_backup(target, backup):
        return "reverted"

    # (B) Recorrected: a later applied change to the same path re-touches us
    with db.conn() as c:
        later = [dict(r) for r in c.execute(
            """SELECT diff, applied_at FROM applied_outcomes
               WHERE target_path = ? AND id > ? AND applied_at > ?
               ORDER BY applied_at ASC""",
            (row["target_path"], row["id"], applied_at),
        ).fetchall()]
    for other in later:
        if _diff_touches_lines(row["diff"], other["diff"]):
            return "recorrected"

    # (C) Lesson re-surfaced: check the lessons_learned table for the
    # same target_path mentioned negatively.
    # Cheap check: any unsent lesson that references this path AND was
    # written after this apply AND has category 'correction' or 'gap'.
    with db.conn() as c:
        surf = c.execute(
            """SELECT 1 FROM lessons_learned
               WHERE created_at > ? AND category IN ('correction', 'gap')
                 AND content LIKE ?
               LIMIT 1""",
            (applied_at, f"%{row['target_path']}%"),
        ).fetchone()
    if surf:
        return "recorrected"

    # (D) Time-based neutral
    if age >= NEUTRAL_AFTER_SECONDS:
        return "neutral"

    return None


def run(limit: int = 50, dry_run: bool = False) -> dict:
    db.init_db()
    pending = db.unknown_outcomes(limit=limit)
    if not pending:
        print("[grade] no unknown outcomes to grade")
        return {"graded": 0, "skipped": 0}

    graded = 0
    skipped = 0
    for row in pending:
        outcome = grade_one(row)
        if outcome is None:
            skipped += 1
            continue
        evidence = _evidence_for(row, outcome)
        if dry_run:
            print(f"  [dry-run] #{row['id']} → {outcome}  {row['target_path']}")
        else:
            db.set_outcome(row["id"], outcome, evidence)
            print(f"  ✓ #{row['id']} → {outcome}  {row['target_path']}")
        graded += 1

    return {"graded": graded, "skipped": skipped, "considered": len(pending)}


def _evidence_for(row: dict, outcome: str) -> str:
    """Short human-readable evidence string for the outcome row."""
    target = row["target_path"]
    if outcome == "reverted":
        return f"file content matches a post-apply backup; {target} was rolled back"
    if outcome == "recorrected":
        return f"later applied change re-touched the same region in {target}"
    if outcome == "neutral":
        return f"no revert/recorrection detected within {NEUTRAL_AFTER_SECONDS // 86400} days; defaulting to neutral"
    return ""


if __name__ == "__main__":
    import argparse
    import json
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=50)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    print(json.dumps(run(limit=args.limit, dry_run=args.dry_run), indent=2))
