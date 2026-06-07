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
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import propose
import judge
import apply
import self_improve
import grade_outcomes


def cycle(skip_apply: bool = False, judge_only: bool = False,
          propose_only: bool = False,
          dry_run: bool = False, max_sessions: int = 3) -> dict:
    db.init_db()
    result: dict = {"steps": []}

    if not judge_only:
        print("=" * 60)
        print("STEP 1: PROPOSE")
        print("=" * 60)
        r = propose.run(max_sessions=max_sessions, dry_run=dry_run)
        result["propose"] = r
        result["steps"].append("propose")
        if propose_only:
            return result

    print("=" * 60)
    print("STEP 2: JUDGE")
    print("=" * 60)
    pending = db.pending_proposals()
    if pending:
        judge.judge_batch(pending)
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
    result["grade"] = g
    result["steps"].append("grade")

    # Self-improve runs on a separate schedule (weekly) — skip in normal cycle
    # to keep individual loops fast.
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
