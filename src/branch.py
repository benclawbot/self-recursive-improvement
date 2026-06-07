"""
branch.py — per-cycle snapshot isolation for apply.py.

The recursive loop modifies ~/.hermes/skills/ and ~/.hermes/memories/
live, in place. A bad change would corrupt user-facing skills until
manually reverted. This module wraps each apply cycle in a snapshot:

  1. cycle_start() — copies the current state of every target file
     that apply.py will touch, into data/branches/<cycle_id>/
  2. record_apply(cycle_id, target) — associates each applied_outcomes
     row with the branch that produced it
  3. revert(cycle_id) — restores every snapshot file, marking the
     applied_outcomes rows as 'reverted' (this is the strongest
     possible "this hurt" signal, and it gives the user a single
     command to undo a bad cycle)

Branch storage: data/branches/<cycle_id>/<mirrored path under HOME>/<file>
e.g. data/branches/2026-06-07T18:30:00Z_42/home/thomas/.hermes/skills/X/SKILL.md

The cycle_id is generated at cycle_start() and threaded through loop.py.

We deliberately avoid 'git init' of ~/.hermes/skills/ — those dirs may
already be part of other repos (clones, worktrees), and nesting .git
breaks things. Filesystem snapshots are the simplest blast-radius
boundary that works with arbitrary target paths.
"""

import os
import json
import shutil
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Iterable

import db

BRANCHES_DIR = Path(__file__).parent.parent / "data" / "branches"
BRANCH_META = "_meta.json"  # written in each branch dir; lists what was snapshotted


def _new_cycle_id() -> str:
    """UTC timestamp + a 6-char random suffix to avoid collisions when
    multiple cycles run in the same second (cron overlap, manual run
    after auto-run, etc)."""
    import secrets
    ts = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
    return f"{ts}_{secrets.token_hex(3)}"


def _mirrored_path(target: Path) -> Path:
    """Translate an absolute target path into its mirror under the branch
    dir. Strips the leading '/' and leading 'home/' to keep paths short
    and human-readable. e.g. /home/thomas/.hermes/skills/X/SKILL.md →
    home/thomas/.hermes/skills/X/SKILL.md
    """
    s = str(target)
    if s.startswith("/"):
        s = s[1:]
    return Path(s)


def cycle_start(targets: Iterable[Path]) -> str:
    """Snapshot every target file that apply.py is about to touch.
    Returns the cycle_id. Call once at the top of apply.run().

    If a target doesn't exist (e.g. memory_add for a new file), we
    record it as 'created_in_cycle' so revert can remove it cleanly
    instead of leaving an empty file behind.
    """
    BRANCHES_DIR.mkdir(parents=True, exist_ok=True)
    cycle_id = _new_cycle_id()
    branch_dir = BRANCHES_DIR / cycle_id
    branch_dir.mkdir(parents=True)

    snapshotted: list[str] = []
    created_in_cycle: list[str] = []

    for target in targets:
        if not target:
            continue
        target_p = Path(target)
        mirror = branch_dir / _mirrored_path(target_p)
        mirror.parent.mkdir(parents=True, exist_ok=True)
        if target_p.exists():
            shutil.copy2(target_p, mirror)
            snapshotted.append(str(target_p))
        else:
            # File will be created in this cycle — record so revert
            # deletes the new file rather than leaving an empty stub.
            (mirror.parent / f".was_missing_{target_p.name}").touch()
            created_in_cycle.append(str(target_p))

    meta = {
        "cycle_id": cycle_id,
        "started_at": time.time(),
        "snapshotted": snapshotted,
        "created_in_cycle": created_in_cycle,
    }
    (branch_dir / BRANCH_META).write_text(json.dumps(meta, indent=2))
    print(f"  [branch] cycle {cycle_id}: snapshotted {len(snapshotted)} files, "
          f"{len(created_in_cycle)} new files marked for cleanup")
    return cycle_id


def attach_to_outcome(applied_outcome_id: int, cycle_id: str) -> None:
    """Bind an applied_outcomes row to the cycle that produced it.
    Done inside apply.py right after record_applied_outcome().
    """
    with db.conn() as c:
        c.execute(
            "UPDATE applied_outcomes SET cycle_id = ? WHERE id = ?",
            (cycle_id, applied_outcome_id),
        )


def revert(cycle_id: str) -> dict:
    """Restore every file in the given branch to its pre-cycle state.
    Marks all linked applied_outcomes rows as 'reverted' (overrides
    whatever the grading heuristics would say) and returns a summary.

    Safe to call multiple times — second call is a no-op (branch dir
    may be partially gone, but the early-exit on missing meta file
    makes the operation idempotent).
    """
    branch_dir = BRANCHES_DIR / cycle_id
    if not branch_dir.exists():
        return {"ok": False, "error": f"branch {cycle_id} not found"}
    meta_path = branch_dir / BRANCH_META
    if not meta_path.exists():
        return {"ok": False, "error": f"branch {cycle_id} missing metadata"}
    meta = json.loads(meta_path.read_text())

    restored = []
    missing_after_revert = []
    for snapshotted_path in meta.get("snapshotted", []):
        target = Path(snapshotted_path)
        mirror = branch_dir / _mirrored_path(target)
        if mirror.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(mirror, target)
            restored.append(snapshotted_path)
        else:
            missing_after_revert.append(snapshotted_path)

    # Files that were created in the cycle should be deleted.
    created_cleaned = []
    for created_path in meta.get("created_in_cycle", []):
        target = Path(created_path)
        if target.exists():
            target.unlink()
            created_cleaned.append(created_path)

    # Mark all linked applied_outcomes rows as reverted. This is the
    # strongest negative signal — overrides grade_outcomes heuristics.
    with db.conn() as c:
        rows = c.execute(
            "SELECT id FROM applied_outcomes WHERE cycle_id = ?",
            (cycle_id,),
        ).fetchall()
        ids = [r["id"] for r in rows]
        if ids:
            placeholders = ",".join("?" * len(ids))
            c.execute(
                f"""UPDATE applied_outcomes
                    SET outcome = 'reverted',
                        outcome_detected_at = ?,
                        outcome_evidence = ?
                    WHERE id IN ({placeholders})""",
                [time.time(),
                 f"manually reverted via branch.py {cycle_id}",
                 *ids],
            )

    return {
        "ok": True,
        "cycle_id": cycle_id,
        "restored": restored,
        "missing_after_revert": missing_after_revert,
        "created_cleaned": created_cleaned,
        "outcomes_marked_reverted": len(ids),
    }


def list_branches() -> list[dict]:
    """List all branches on disk with a one-line summary. Used by the
    digest and by the --branches CLI flag.
    """
    if not BRANCHES_DIR.exists():
        return []
    out = []
    for d in sorted(BRANCHES_DIR.iterdir(), reverse=True):
        if not d.is_dir():
            continue
        meta_path = d / BRANCH_META
        if not meta_path.exists():
            out.append({"cycle_id": d.name, "meta_missing": True})
            continue
        meta = json.loads(meta_path.read_text())
        # How many applied_outcomes rows are linked to this branch?
        with db.conn() as c:
            n_applied = c.execute(
                "SELECT COUNT(*) AS n FROM applied_outcomes WHERE cycle_id = ?",
                (d.name,),
            ).fetchone()["n"]
            n_reverted = c.execute(
                "SELECT COUNT(*) AS n FROM applied_outcomes WHERE cycle_id = ? AND outcome = 'reverted'",
                (d.name,),
            ).fetchone()["n"]
        out.append({
            "cycle_id": d.name,
            "started_at": meta.get("started_at"),
            "files_snapshotted": len(meta.get("snapshotted", [])),
            "files_created": len(meta.get("created_in_cycle", [])),
            "applied_rows": n_applied,
            "reverted_rows": n_reverted,
        })
    return out


def prune_old_branches(keep: int = 20) -> int:
    """Delete the oldest branches, keeping the most recent `keep`.
    Branches whose outcomes are all graded (not 'unknown') are safe to
    drop — the audit trail lives in applied_outcomes regardless.

    Returns the number of branches pruned. Called by the digest
    (weekly) or manually via --prune-branches.
    """
    if not BRANCHES_DIR.exists():
        return 0
    branches = sorted(
        (d for d in BRANCHES_DIR.iterdir() if d.is_dir()),
        key=lambda d: d.name,  # ISO timestamp prefix → lexicographic = chronological
        reverse=True,
    )
    surplus = branches[keep:]
    n = 0
    for b in surplus:
        # Safety: only prune if all linked outcomes are graded or reverted
        meta_path = b / BRANCH_META
        if meta_path.exists():
            with db.conn() as c:
                row = c.execute(
                    """SELECT COUNT(*) AS unknown_n FROM applied_outcomes
                       WHERE cycle_id = ? AND outcome = 'unknown'""",
                    (b.name,),
                ).fetchone()
                if row["unknown_n"] > 0:
                    continue  # don't drop a branch with ungraded work
        shutil.rmtree(b, ignore_errors=True)
        n += 1
    return n


if __name__ == "__main__":
    import argparse, json as _json
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--revert", metavar="CYCLE_ID")
    ap.add_argument("--prune", type=int, default=0, metavar="KEEP",
                    help="prune oldest branches, keeping KEEP most recent")
    args = ap.parse_args()
    if args.list:
        print(_json.dumps(list_branches(), indent=2))
    elif args.revert:
        print(_json.dumps(revert(args.revert), indent=2))
    elif args.prune:
        print(f"pruned: {prune_old_branches(args.prune)}")
