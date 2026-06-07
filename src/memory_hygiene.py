"""
memory_hygiene.py — Phase 2 stale-lesson detection.

Scans `lessons_learned` for entries that have either:
  - a source session that no longer exists in ~/.hermes/sessions/
  - never been sent in a digest AND was created > 30 days ago

Idempotent. Never auto-deletes. Surfaces candidates for thomas's
manual review. The proposer's next cycle sees them via db.list_stale_candidates
so it can propose a removal or refresh with new evidence.
"""

import db


DEFAULT_DAYS = 30


def run(days: int = DEFAULT_DAYS, dry_run: bool = False, limit: int = 50) -> dict:
    """Detect stale memory candidates. Returns summary dict."""
    flagged = db.detect_stale_memory(days=days)
    if dry_run:
        print(f"[memory-hygiene] DRY RUN — would flag {len(flagged)} new candidate(s)")
        for f in flagged[:limit]:
            print(f"  lesson #{f['lesson_id']} ({f['reason']}): {f['content'][:80]}")
        return {"flagged": len(flagged), "preview": flagged[:limit]}

    print(f"[memory-hygiene] Flagged {len(flagged)} new candidate(s) "
          f"(threshold: {days} days)")
    for f in flagged[:limit]:
        print(f"  lesson #{f['lesson_id']} ({f['reason']}): {f['content'][:80]}")
    stats = db.stale_memory_stats()
    return {"flagged": len(flagged), "total_candidates": stats}


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = run(days=args.days, dry_run=args.dry_run)
    import json
    print(json.dumps(result, indent=2, default=str))
