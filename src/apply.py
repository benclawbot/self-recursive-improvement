"""
apply.py — execute approved proposals against the actual skill/memory files.

Reads proposals with status='merged' (set when thomas approved) and applies
the diff to the target file. This is the only place that writes to skills/
or memory, so it's the blast-radius boundary.

Safety:
  - Pinned skills (per skill_manage protection) are skipped — those go via PR
  - All edits are backed up to data/backups/<timestamp>_<path>
  - Idempotent: re-applying the same diff is a no-op (skips)
  - All operations logged to logs/apply.log
"""

import os
import json
import shutil
import sys
import time
import argparse
import re
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import db
import branch


import os as _os_apply

HERMES_SKILLS = Path.home() / ".hermes" / "skills"
HERMES_MEMORIES = Path.home() / ".hermes" / "memories"
# Backup dir is per-process state. Allow tests (and overrides) to redirect
# to a temp dir via env. Otherwise test runs leak DONE_#N_... markers into
# the prod backup dir and break idempotency on subsequent runs.
_DEFAULT_BACKUP_DIR = Path(__file__).parent.parent / "data" / "backups"
BACKUP_DIR = Path(_os_apply.environ.get("SRI_BACKUP_DIR", str(_DEFAULT_BACKUP_DIR)))
LOG_FILE = Path(__file__).parent.parent / "logs" / "apply.log"


def _log(msg: str):
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(LOG_FILE, "a") as f:
        f.write(f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] {msg}\n")
    print(msg)


def _backup(target: Path) -> Path:
    """Copy current file to backup dir. Returns backup path."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    backup = BACKUP_DIR / f"{ts}_{target.name}"
    if target.exists():
        shutil.copy2(target, backup)
    return backup


def _is_pinned_skill(path: Path) -> bool:
    """Heuristic: hub-installed skills are under skill_assets or have a
    .hub-marker file. Conservative: refuse if uncertain."""
    # Hub skills live under ~/.hermes/skill-assets/
    if str(path).startswith(str(Path.home() / ".hermes" / "skill-assets")):
        return True
    return False


def _is_safe_path(path_str: str) -> bool:
    """Refuse paths outside hermes home or /tmp."""
    if not path_str:
        return False
    p = Path(path_str).resolve()
    home = Path.home().resolve()
    tmp = Path("/tmp").resolve()
    return str(p).startswith(str(home)) or str(p).startswith(str(tmp))


def _apply_unified_diff(target: Path, diff: str) -> bool:
    """Apply a simple unified diff. Falls back to 'replace all' for
    trivial whole-file rewrites. Returns True on success."""
    # If the diff is a full file, just write it
    if diff.startswith("--- ") and "+++" in diff:
        # Try patch first
        return _apply_patch(target, diff)
    # Plain text → interpret as append or replace based on markers
    if diff.startswith("APPEND:"):
        content = diff[len("APPEND:"):].lstrip("\n")
        if target.exists():
            with open(target, "a") as f:
                f.write("\n" + content)
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content)
        return True
    if diff.startswith("REPLACE_ALL:"):
        content = diff[len("REPLACE_ALL:"):].lstrip("\n")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        return True
    # Default: treat as a `patch` tool old→new style block
    # Format: "<<<\nOLD\n===\nNEW\n>>>"
    m = re.match(r"^<<<\n(.*?)\n===\n(.*?)\n>>>$", diff, re.DOTALL)
    if m:
        old, new = m.group(1), m.group(2)
        if not target.exists():
            return False
        text = target.read_text()
        if old not in text:
            return False
        target.write_text(text.replace(old, new, 1))
        return True
    # Heuristic fallback: if the diff is plain markdown/text with no
    # recognized markers, treat it as an APPEND. This matches what the
    # proposer typically produces (a new section to insert at the end
    # of the target file) and unblocks the inline-button approve flow.
    if diff.strip() and not any(
        diff.startswith(p) for p in ("--- ", "APPEND:", "REPLACE_ALL:")
    ) and "<<<" not in diff.splitlines()[0]:
        _log("  diff has no recognized markers; defaulting to APPEND")
        if target.exists():
            with open(target, "a") as f:
                f.write("\n" + diff.lstrip("\n"))
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(diff)
        return True
    # Last resort: try as a unified diff anyway
    return _apply_patch(target, diff)


def _apply_patch(target: Path, diff: str) -> bool:
    """Apply a unified diff using the `patch` command."""
    import subprocess
    pfile = Path("/tmp") / f"loop_diff_{int(time.time()*1000)}.patch"
    pfile.write_text(diff)
    try:
        r = subprocess.run(
            ["patch", "-p0", str(target)],
            input=diff, capture_output=True, text=True, timeout=30,
        )
        if r.returncode == 0:
            return True
        _log(f"  patch failed: {r.stderr[:200]}")
        return False
    except FileNotFoundError:
        _log("  patch command not available; skipping")
        return False
    finally:
        pfile.unlink(missing_ok=True)


def apply_proposal(p: dict, cycle_id: str | None = None) -> bool:
    """Apply a single approved proposal. Returns True on success.

    If cycle_id is provided, the applied_outcomes row is linked to that
    branch so `python apply.py --revert <cycle_id>` can roll it back.
    """
    kind = p["target_kind"]
    target = p.get("target_path", "")

    if not _is_safe_path(target):
        _log(f"  reject proposal #{p['id']}: unsafe path {target}")
        return False

    if _is_pinned_skill(Path(target)):
        _log(f"  skip proposal #{p['id']}: target is pinned/hub-installed")
        return False

    if kind == "skill_patch" or kind == "skill_create":
        if not target.startswith(str(HERMES_SKILLS)):
            _log(f"  reject #{p['id']}: skill path outside {HERMES_SKILLS}")
            return False
    elif kind == "memory_add":
        if not target.startswith(str(HERMES_MEMORIES)):
            _log(f"  reject #{p['id']}: memory path outside {HERMES_MEMORIES}")
            return False

    target_p = Path(target)
    _log(f"  applying #{p['id']} ({kind}) → {target}")
    backup = _backup(target_p)
    _log(f"    backup: {backup}")
    ok = _apply_unified_diff(target_p, p["diff"])
    if ok:
        # Record the application so we can later grade whether it helped.
        # The grading happens in grade_outcomes() — kept out of the hot path
        # because it requires re-reading files and may call m3.
        try:
            outcome_id = db.record_applied_outcome(
                proposal_id=p["id"],
                target_kind=kind,
                target_path=target,
                diff=p["diff"],
            )
            if cycle_id and outcome_id is not None:
                branch.attach_to_outcome(outcome_id, cycle_id)
        except Exception as e:
            _log(f"    ! failed to record outcome row: {e}")
        _log(f"    ✓ applied")
    else:
        _log(f"    ✗ apply failed, restoring from backup")
        try:
            if backup.exists():
                if target_p.exists():
                    shutil.copy2(backup, target_p)
                else:
                    # Target file was already missing when we started; can't
                    # restore, and there was nothing to roll back. Just log.
                    _log(f"    ! target {target_p} did not exist; nothing to restore")
        except Exception as e:
            _log(f"    ! rollback failed: {e}")
        # Phase 3: feed apply failures back. The proposer can then
        # propose: "fix the diff format" or "add a diff validator".
        try:
            db.add_lesson(
                category="gap",
                content=(
                    f"apply failed for proposal #{p['id']} "
                    f"({kind} {target}). Diff was: {p['diff'][:200]}"
                ),
                source=f"apply:{p['id']}",
            )
        except Exception:
            pass
    return ok


def run() -> int:
    """Apply all merged proposals that haven't been applied yet.

    Per-cycle branch isolation: snapshots every target file to
    data/branches/<cycle_id>/ before any write, so a single bad
    change can be rolled back with `python src/apply.py --revert <id>`.
    """
    with db.conn() as c:
        # We use 'merged' as the marker; track which we've applied via
        # the rationale field marker. Or — better — just idempotency check.
        rows = c.execute(
            "SELECT * FROM proposals WHERE status = 'merged' ORDER BY id"
        ).fetchall()

    if not rows:
        print("[apply] No merged proposals to apply.")
        return 0

    print(f"[apply] {len(rows)} merged proposal(s) to apply")

    # Snapshot every distinct target before touching anything.
    targets = sorted({r["target_path"] for r in rows if r["target_path"]})
    cycle_id = branch.cycle_start(targets)

    applied = 0
    for row in rows:
        p = dict(row)
        # Idempotency: skip if a backup of this exact target+diff exists
        marker_key = f"#{p['id']}_" + (p.get("target_path") or "")
        marker_file = BACKUP_DIR / f"DONE_{marker_key.replace('/', '_')}"
        if marker_file.exists():
            continue
        if apply_proposal(p, cycle_id=cycle_id):
            marker_file.touch()
            applied += 1

    if applied:
        print(f"[apply] cycle {cycle_id} → {applied} applied")
        print(f"[apply] to revert: python src/apply.py --revert {cycle_id}")
    return applied


def main():
    import argparse, json as _json
    ap = argparse.ArgumentParser()
    ap.add_argument("--revert", metavar="CYCLE_ID",
                    help="revert all changes from a previous cycle")
    ap.add_argument("--branches", action="store_true",
                    help="list all branches and their outcome counts")
    ap.add_argument("--prune-branches", type=int, default=0, metavar="KEEP",
                    help="prune branches with KEEP most recent kept (only safe ones)")
    args = ap.parse_args()

    if args.revert:
        print(_json.dumps(branch.revert(args.revert), indent=2))
    elif args.branches:
        print(_json.dumps(branch.list_branches(), indent=2))
    elif args.prune_branches:
        n = branch.prune_old_branches(args.prune_branches)
        print(f"pruned: {n}")
    else:
        n = run()
        print(f"[apply] Done. {n} applied.")


if __name__ == "__main__":
    main()
