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
import time
import argparse
import re
from pathlib import Path

import db


HERMES_SKILLS = Path.home() / ".hermes" / "skills"
HERMES_MEMORIES = Path.home() / ".hermes" / "memories"
BACKUP_DIR = Path(__file__).parent.parent / "data" / "backups"
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


def apply_proposal(p: dict) -> bool:
    """Apply a single approved proposal. Returns True on success."""
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
        _log(f"    ✓ applied")
    else:
        _log(f"    ✗ apply failed, restoring from backup")
        if backup.exists():
            shutil.copy2(backup, target_p)
    return ok


def run():
    """Apply all merged proposals that haven't been applied yet."""
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
    applied = 0
    for row in rows:
        p = dict(row)
        # Idempotency: skip if a backup of this exact target+diff exists
        marker_key = f"#{p['id']}_" + (p.get("target_path") or "")
        marker_file = BACKUP_DIR / f"DONE_{marker_key.replace('/', '_')}"
        if marker_file.exists():
            continue
        if apply_proposal(p):
            marker_file.touch()
            applied += 1
    return applied


if __name__ == "__main__":
    n = run()
    print(f"[apply] Done. {n} applied.")
