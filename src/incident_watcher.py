"""
incident_watcher.py — Phase 4 self-referential incident detection.

Watches the loop's own log files for signs of failure:
  - Log files older than 4h with a recent timestamp → job died silently
  - Last log line indicates a non-zero exit
  - Cycle wall-time > 110s (approaching the 120s cron cap)

On any detection, write a `self_incidents` row. The proposer then
mines it as a synthetic session and can propose a fix.

Run as a separate cron job, every 6h. Cheap (just file mtimes + tail).
"""

import os
import time
from pathlib import Path

import db

LOGS_DIR = Path(__file__).parent.parent / "logs"
JOB_LOG_PATTERNS = {
    "sri-propose": "propose_",
    "sri-judge": "judge_",
    "sri-cycle": "cycle_",  # legacy name
    "sri-apply": "apply.log",  # different shape — see below
}
# Per-job staleness threshold: 1.5x the cron schedule + 1h buffer.
# sri-propose / sri-judge run every 8h → 13h staleness threshold
# sri-cycle ran every 8h (legacy) → 13h
# sri-apply runs every 24h (daily 4am) → 37h
JOB_STALE_THRESHOLDS = {
    "sri-propose": 13 * 3600,
    "sri-judge": 13 * 3600,
    "sri-cycle": 13 * 3600,
    "sri-apply": 37 * 3600,
}
WALL_TIME_THRESHOLD_MS = 110_000   # 110s
INCIDENT_DEDUP_SECONDS = 24 * 3600  # don't re-flag same incident within 24h


def _latest_log_for_job(job_name: str) -> Path | None:
    """Find the most recent log file for a given job."""
    pattern = JOB_LOG_PATTERNS.get(job_name)
    if not pattern:
        return None
    if pattern.endswith(".log"):
        # Static log file (apply.log)
        candidate = LOGS_DIR / pattern
        return candidate if candidate.exists() else None
    # Timestamped log files like cycle_20260607_060005.log
    candidates = sorted(LOGS_DIR.glob(f"{pattern}*.log"), key=lambda p: p.stat().st_mtime, reverse=True)
    return candidates[0] if candidates else None


def _tail_lines(path: Path, n: int = 20) -> str:
    """Return the last N lines of a file. Cheap."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)  # end
            size = f.tell()
            # Read last 4KB max
            f.seek(max(0, size - 4096))
            data = f.read().decode("utf-8", errors="replace")
        lines = data.splitlines()[-n:]
        return "\n".join(lines)
    except OSError:
        return ""


def _already_reported(job_name: str, incident_type: str) -> bool:
    """Has this exact incident been reported in the last 24h?"""
    with db.conn() as c:
        row = c.execute(
            """SELECT 1 FROM self_incidents
               WHERE job_name = ? AND incident_type = ?
                 AND detected_at > ? LIMIT 1""",
            (job_name, incident_type, time.time() - INCIDENT_DEDUP_SECONDS),
        ).fetchone()
    return row is not None


def _record_incident(job_name: str, incident_type: str, detail: str, log_tail: str):
    if _already_reported(job_name, incident_type):
        print(f"  skip {job_name}/{incident_type} — already reported in last 24h")
        return
    with db.conn() as c:
        c.execute(
            """INSERT INTO self_incidents
               (detected_at, job_name, incident_type, detail, last_log_lines)
               VALUES (?, ?, ?, ?, ?)""",
            (time.time(), job_name, incident_type, detail, log_tail[:2000]),
        )
    print(f"  ⚠ {job_name}/{incident_type}: {detail}")


def check_job(job_name: str) -> int:
    """Check a single job's log for incidents. Returns count of new incidents."""
    log = _latest_log_for_job(job_name)
    if not log:
        return 0
    try:
        mtime = log.stat().st_mtime
    except OSError:
        return 0

    age = time.time() - mtime
    threshold = JOB_STALE_THRESHOLDS.get(job_name, 13 * 3600)
    if age > threshold:
        # Log file is stale — job hasn't run within its expected cadence
        # (this is the failure mode that bit us at 120s timeout)
        _record_incident(
            job_name=job_name,
            incident_type="no_output",
            detail=f"log {log.name} last modified {age/3600:.1f}h ago (threshold {threshold/3600:.0f}h)",
            log_tail=_tail_lines(log, 20),
        )
        return 1

    # Check for high wall time in cycle_stats
    if job_name in ("sri-propose", "sri-cycle", "sri-judge"):
        with db.conn() as c:
            row = c.execute(
                """SELECT duration_ms, started_at FROM cycle_stats
                   ORDER BY started_at DESC LIMIT 1"""
            ).fetchone()
        if row and row["duration_ms"] > WALL_TIME_THRESHOLD_MS:
            _record_incident(
                job_name=job_name,
                incident_type="wall_time_high",
                detail=f"last cycle {row['duration_ms']/1000:.1f}s (threshold {WALL_TIME_THRESHOLD_MS/1000:.0f}s)",
                log_tail=_tail_lines(log, 20),
            )
            return 1

    return 0


def run(jobs: list | None = None, dry_run: bool = False) -> dict:
    """Check all loop jobs. Returns summary."""
    if jobs is None:
        jobs = list(JOB_LOG_PATTERNS.keys())
    db.init_db()
    total = 0
    for j in jobs:
        n = check_job(j)
        total += n
    print(f"[incident-watcher] {total} new incident(s) flagged across {len(jobs)} job(s)")
    return {"new_incidents": total, "jobs_checked": len(jobs)}


def list_recent_incidents(limit: int = 5) -> list:
    """For the proposer / digest."""
    with db.conn() as c:
        return [dict(r) for r in c.execute(
            """SELECT * FROM self_incidents
               ORDER BY detected_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()]


def unmined_incident_as_session() -> dict | None:
    """Synthesize a 'session' for the proposer to mine. Returns None if
    no recent unmined incident. The proposer treats this like a real
    session — it can propose a fix.

    Idempotent: marks the incident as 'consumed' (we add a session_mined
    row pointing at a synthetic session id) so we don't re-feed the
    same incident to the proposer across multiple cycles.
    """
    with db.conn() as c:
        row = c.execute(
            """SELECT * FROM self_incidents
               WHERE detected_at > ? AND id NOT IN (
                 SELECT CAST(SUBSTR(session_id, 10) AS INTEGER)
                 FROM sessions_mined
                 WHERE session_id LIKE 'incident_%'
               )
               ORDER BY detected_at DESC LIMIT 1""",
            (time.time() - 7 * 86400,),  # last 7 days
        ).fetchone()
    if not row:
        return None
    return dict(row)


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    import json
    result = run(dry_run=args.dry_run)
    print(json.dumps(result, indent=2))
