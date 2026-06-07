"""
loop.py — orchestrator. Run one full cycle of the self-improvement loop.

A cycle:
  1. propose: mine sessions → m3 generates candidates
  2. judge:   m2.7 reviews each candidate against active rubric
  3. apply:   merge approved proposals to skill/memory files
  4. self-improve: if override rate is high, propose rubric refinement

This is what the cron job calls. For ad-hoc runs:

    python src/loop.py --skip-apply   # propose + judge only
    python src/loop.py --judge-only   # re-judge pending proposals
    python src/loop.py --dry-run      # no writes anywhere
"""

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import propose
import judge
import apply
import self_improve
import grade_outcomes
import memory_hygiene
import incident_watcher
import checkpoint


def cycle(skip_apply: bool = False, judge_only: bool = False,
          propose_only: bool = False,
          dry_run: bool = False, max_sessions: int = 3) -> dict:
    started_at = time.time()
    db.init_db()
    # Load prior judge calibration state (cheap, restores baseline if
    # the process restarted). Stale (>24h) is reported as stale, not silent.
    prior = checkpoint.load()
    if prior:
        print(f"  [checkpoint] loaded prior state: {checkpoint.fmt_for_log(prior)}")
        if prior.get("_stale"):
            print(f"  [checkpoint] WARNING: state is {prior.get('age_hours', 0):.1f}h old, treating as stale")
    else:
        print("  [checkpoint] no prior state on disk; calibrating from scratch")
    result: dict = {"steps": [], "_started_at": started_at}
    counters = {
        "proposals_mined": 0,
        "proposals_generated": 0,
        "judge_calls": 0,
        "grade_graded": 0,
        "grade_skipped": 0,
    }

    if not judge_only:
        print("=" * 60)
        print("STEP 1: PROPOSE")
        print("=" * 60)
        r = propose.run(max_sessions=max_sessions, dry_run=dry_run)
        result["propose"] = r
        counters["proposals_mined"] = r.get("mined", 0)
        counters["proposals_generated"] = r.get("proposed", 0)
        result["steps"].append("propose")
        if propose_only:
            return _finalize(result, counters, started_at)

    print("=" * 60)
    print("STEP 2: JUDGE")
    print("=" * 60)
    pending = db.pending_proposals()
    if pending:
        judge.judge_batch(pending)
        counters["judge_calls"] = len(pending)
    else:
        print("  nothing to judge")
    result["judge"] = {"count": len(pending)}
    result["steps"].append("judge")

    if not skip_apply and not dry_run:
        print("=" * 60)
        print("STEP 3: APPLY (merged proposals only)")
        print("=" * 60)
        n = apply.run()
        result["apply"] = {"applied": n}
        result["steps"].append("apply")

    # Step 3.5: grade prior outcomes. Cheap (no LLM) so we run it
    # every cycle. 'unknown' rows that aren't old enough get skipped.
    print("=" * 60)
    print("STEP 3.5: GRADE OUTCOMES")
    print("=" * 60)
    if not dry_run:
        g = grade_outcomes.run(limit=20)
    else:
        g = grade_outcomes.run(limit=20, dry_run=True)
    counters["grade_graded"] = g.get("graded", 0)
    counters["grade_skipped"] = g.get("skipped", 0)
    result["grade"] = g
    result["steps"].append("grade")

    # Step 3.6: memory hygiene (Phase 2). Scan for stale lessons.
    # Cheap (no LLM), runs every cycle. Surfaces candidates for
    # thomas, never auto-deletes.
    print("=" * 60)
    print("STEP 3.6: MEMORY HYGIENE")
    print("=" * 60)
    if not dry_run:
        m = memory_hygiene.run()
    else:
        m = memory_hygiene.run(dry_run=True)
    result["memory_hygiene"] = m
    result["steps"].append("memory_hygiene")

    # Step 3.7: incident watcher (Phase 4). Cheap check for cron
    # job failures / silent deaths / wall-time spikes. Writes
    # self_incidents; proposer sees them next cycle.
    print("=" * 60)
    print("STEP 3.7: INCIDENT WATCHER")
    print("=" * 60)
    if not dry_run:
        iw = incident_watcher.run()
    else:
        iw = incident_watcher.run(dry_run=True)
    result["incident_watcher"] = iw
    result["steps"].append("incident_watcher")

    return _finalize(result, counters, started_at)


def _finalize(result: dict, counters: dict, started_at: float) -> dict:
    """Strip internal fields, record cycle_stats, return cleaned result."""
    finished_at = time.time()
    error = result.get("_error")
    try:
        if not result.get("_dry_run"):
            db.record_cycle_stats(
                started_at=started_at,
                finished_at=finished_at,
                steps={s: True for s in result.get("steps", [])},
                **counters,
                error=error,
            )
            # Save judge calibration checkpoint. Skip propose-only runs
            # (no judge step) and dry-runs (no real data). Real cycles
            # + judge-only runs both write fresh state.
            steps_run = set(result.get("steps", []))
            if "judge" in steps_run:
                try:
                    saved = checkpoint.save()
                    print(f"  [checkpoint] saved: {checkpoint.fmt_for_log(saved)}")
                except Exception as ckpt_exc:
                    print(f"  [checkpoint] save failed (non-fatal): {ckpt_exc}")
    except Exception as e:
        # Last-resort: don't let the stats-recorder break the cycle
        print(f"[loop] warning: failed to record cycle_stats: {e}")
    # Strip internal fields before returning
    result.pop("_started_at", None)
    result.pop("_dry_run", None)
    result.pop("_error", None)
    return result


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--skip-apply", action="store_true")
    ap.add_argument("--judge-only", action="store_true")
    ap.add_argument("--propose-only", action="store_true",
                    help="run only the propose step; exit before judge")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--max", type=int, default=3, help="max sessions to mine")
    ap.add_argument("--self-improve", action="store_true",
                    help="run rubric self-eval (weekly cadence)")
    args = ap.parse_args()

    r = cycle(skip_apply=args.skip_apply, judge_only=args.judge_only,
              propose_only=args.propose_only,
              dry_run=args.dry_run, max_sessions=args.max)
    print()
    print("Cycle result:", r)

    if args.self_improve:
        print()
        print("=" * 60)
        print("STEP 4: SELF-IMPROVE (rubric eval)")
        print("=" * 60)
        self_improve.run(dry_run=args.dry_run)
