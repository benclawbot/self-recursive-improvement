"""
Judge checkpoint — durability for in-process calibration state.

Problem: the judge process is stateless across restarts. The SQLite tables
record *what* the judge did (proposals, verdicts, thomas feedback, applied
outcomes), but the *baseline calibration* (override rate over last 50 cycles,
help ratio, judge-error rate, recent drift) is computed fresh on each run.

If the judge process restarts mid-evening, the next run's threshold logic
sees "no recent history" and either over- or under-rejects. A 5kb JSON
file fixes this: write after each cycle, load on startup.

What's in the checkpoint:
  - override_rate_50: thomas override rate over last 50 verdicts
  - help_ratio_50: applied-and-graded outcomes with outcome='help' / total
  - judge_error_rate_50: judge API call failures / total over last 50
  - drift_score: difference between override_rate_50 and a 200-row baseline
  - last_cycle_at: unix timestamp of last successful cycle
  - cycles_total: monotonic counter

What's NOT in the checkpoint:
  - any proposal / verdict content (already in SQLite)
  - the rubric (already in SQLite)
  - the proposals table itself
"""

import json
import os
import time
from pathlib import Path

import db


CHECKPOINT_PATH = Path(__file__).parent.parent / "data" / "judge_state.json"
WINDOW = 50          # short window for "recent" calibration
BASELINE_WINDOW = 200  # longer window for drift detection


def _ensure_parent() -> None:
    CHECKPOINT_PATH.parent.mkdir(parents=True, exist_ok=True)


def _override_rate(limit: int) -> float | None:
    """Thomas override rate = (rejects where thomas overrode to approve)
    + (approves where thomas overrode to reject) / total verdicts with feedback."""
    with db.conn() as c:
        row = c.execute(
            """SELECT
                 SUM(CASE WHEN tf.verdict != jv.verdict THEN 1 ELSE 0 END) AS overrides,
                 COUNT(tf.id) AS with_feedback
               FROM thomas_feedback tf
               JOIN judge_verdicts jv ON jv.proposal_id = tf.proposal_id
               WHERE tf.feedback_at >= (
                 SELECT feedback_at FROM thomas_feedback
                 ORDER BY id DESC LIMIT 1 OFFSET ?
               )""",
            (limit - 1,),
        ).fetchone()
    if not row or not row["with_feedback"]:
        return None
    return row["overrides"] / row["with_feedback"]


def _help_ratio(limit: int) -> float | None:
    """helped / (helped + reverted + recorrected) over last N graded outcomes.
    Neutral is excluded from denominator (not informative). 'unknown'
    is excluded entirely (not yet graded)."""
    with db.conn() as c:
        rows = c.execute(
            """SELECT outcome, COUNT(*) AS n
               FROM applied_outcomes
               WHERE outcome_detected_at IS NOT NULL
                 AND outcome IN ('helped', 'reverted', 'recorrected')
                 AND id >= COALESCE(
                   (SELECT id FROM applied_outcomes
                    WHERE outcome_detected_at IS NOT NULL
                    ORDER BY id DESC LIMIT 1 OFFSET ?), 0)
               GROUP BY outcome""",
            (limit - 1,),
        ).fetchall()
    counts = {r["outcome"]: r["n"] for r in rows}
    total = counts.get("helped", 0) + counts.get("reverted", 0) + counts.get("recorrected", 0)
    if total == 0:
        return None
    return counts.get("helped", 0) / total


def _judge_error_rate(limit: int) -> float | None:
    with db.conn() as c:
        row = c.execute(
            """SELECT
                 SUM(CASE WHEN judge_error IS NOT NULL AND judge_error != '' THEN 1 ELSE 0 END) AS errs,
                 COUNT(*) AS total
               FROM judge_verdicts
               WHERE judged_at >= (
                 SELECT judged_at FROM judge_verdicts
                 ORDER BY id DESC LIMIT 1 OFFSET ?
               )""",
            (limit - 1,),
        ).fetchone()
    if not row or not row["total"]:
        return None
    return row["errs"] / row["total"]


def _drift_score() -> float | None:
    """Short-window override rate minus long-window baseline.
    Positive = judging worse than baseline. Negative = better."""
    short = _override_rate(WINDOW)
    long = _override_rate(BASELINE_WINDOW)
    if short is None or long is None:
        return None
    return short - long


def _cycles_total() -> int:
    with db.conn() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM cycle_stats").fetchone()
    return row["n"] if row else 0


def compute_state() -> dict:
    """Build the checkpoint state dict from current DB."""
    return {
        "schema_version": 1,
        "computed_at": time.time(),
        "cycles_total": _cycles_total(),
        "override_rate_50": _override_rate(WINDOW),
        "override_rate_200": _override_rate(BASELINE_WINDOW),
        "drift_score": _drift_score(),
        "help_ratio_50": _help_ratio(WINDOW),
        "judge_error_rate_50": _judge_error_rate(WINDOW),
    }


def save() -> dict:
    """Dump current state to disk atomically. Returns the state."""
    state = compute_state()
    _ensure_parent()
    tmp = CHECKPOINT_PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, indent=2, sort_keys=True))
    os.replace(tmp, CHECKPOINT_PATH)  # atomic on POSIX
    return state


def load() -> dict | None:
    """Read state from disk. Returns None if missing/corrupt."""
    if not CHECKPOINT_PATH.exists():
        return None
    try:
        return json.loads(CHECKPOINT_PATH.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def merge_with_freshness() -> dict:
    """Load disk state; if it's older than 24h, treat as stale and return None
    (caller should fall back to live computation)."""
    s = load()
    if s is None:
        return None
    age_h = (time.time() - s.get("computed_at", 0)) / 3600
    if age_h > 24:
        merged: dict = {"_stale": True, "age_hours": age_h, **s}
        return merged
    return s


def fmt_for_log(state: dict | None) -> str:
    """Compact one-line summary for cron logs."""
    if not state:
        return "checkpoint: none"
    parts = [
        f"cycles={state.get('cycles_total', '?')}",
    ]
    for k in ("override_rate_50", "help_ratio_50", "judge_error_rate_50", "drift_score"):
        v = state.get(k)
        if v is not None:
            parts.append(f"{k}={v:.2f}")
    return "checkpoint: " + " ".join(parts)


if __name__ == "__main__":
    s = save()
    print(fmt_for_log(s))
    print(f"wrote {CHECKPOINT_PATH}")
