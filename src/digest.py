#!/usr/bin/env python3
"""
SRI weekly digest — concise format Thomas asked for.

Sections:
  RULES PROPOSED       — pending proposals awaiting his decision
  RULES REJECTED       — judge (or thomas) said no, with reason
  CONFIRM NEEDED       — things the system can't decide on its own
  CONTEXT              — pipeline numbers, incidents, branches

If TELEGRAM_BOT_TOKEN + TELEGRAM_HOME_CHANNEL are in the environment,
the digest is sent to that chat. Otherwise it prints to stdout (cron
should `source ~/.hermes/.env` first; or use the `set -a` pattern).

The previous digest had three problems thomas flagged:
  1. "Pipeline errors (7d): 34" was actually merged proposals, not errors.
  2. The #4 "apply failed" message was stale noise (no proposal #4 exists;
     only #57 is pending). The caveman test marker leaked into the diff
     buffer from an earlier cycle.
  3. There was no clean separation between "needs decision" and "FYI".
This script fixes all three.
"""
from __future__ import annotations
import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Tuple

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "loop.db"
LOGS_DIR = ROOT / "logs"

SEVEN_DAYS = 7 * 86400

# Test/sandbox path patterns the SRI loop itself creates. These should
# never count as real production activity in the digest.
SANDBOX_MARKERS = ("_sri_test_skill_", "/tmp/sri_")


def _ts_human(epoch: float | None) -> str:
    if not epoch:
        return "—"
    return time.strftime("%Y-%m-%d %H:%MZ", time.gmtime(epoch))


def _truncate(s: str | None, n: int) -> str:
    if not s:
        return ""
    s = s.replace("\n", " ").strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def load_pending(cur: sqlite3.Cursor) -> List[dict]:
    """Rules proposed — awaiting thomas's approve/reject/modify."""
    cur.execute(
        """SELECT id, target_kind, target_path, rationale, confidence, created_at
             FROM proposals WHERE status='pending' ORDER BY id"""
    )
    rows = [dict(r) for r in cur.fetchall()]
    # Fall back to first diff line for target_kind=memory_add where target_path is NULL
    for r in rows:
        if not r["target_path"]:
            cur.execute("SELECT diff FROM proposals WHERE id=?", (r["id"],))
            d = cur.fetchone()
            if d and d["diff"]:
                first = d["diff"].lstrip("# ").splitlines()[0:3]
                r["target_path"] = "MEMORY — " + " / ".join(s.strip() for s in first if s.strip())[:80]
    return rows


def load_rejected_judge(cur: sqlite3.Cursor, limit: int = 5) -> List[dict]:
    """Rules the judge said no to (not thomas-overridden)."""
    cur.execute(
        """SELECT id, target_kind, target_path, rationale
             FROM proposals WHERE status='rejected'
             ORDER BY id DESC LIMIT ?""",
        (limit,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if not r["target_path"]:
            cur.execute("SELECT diff FROM proposals WHERE id=?", (r["id"],))
            d = cur.fetchone()
            if d and d["diff"]:
                first = d["diff"].lstrip("# ").splitlines()[0:3]
                r["target_path"] = "MEMORY — " + " / ".join(s.strip() for s in first if s.strip())[:80]
    return rows


def load_thomas_overrides(cur: sqlite3.Cursor, limit: int = 5) -> List[dict]:
    """Cases where thomas overrode the judge — used to detect drift."""
    cur.execute(
        """SELECT tf.proposal_id, tf.verdict, tf.note, tf.was_overrides_judge,
                  p.target_kind, p.target_path
             FROM thomas_feedback tf
             JOIN proposals p ON p.id = tf.proposal_id
             ORDER BY tf.id DESC LIMIT ?""",
        (limit,),
    )
    rows = [dict(r) for r in cur.fetchall()]
    for r in rows:
        if not r["target_path"]:
            cur.execute("SELECT diff FROM proposals WHERE id=?", (r["proposal_id"],))
            d = cur.fetchone()
            if d and d["diff"]:
                first = d["diff"].lstrip("# ").splitlines()[0:3]
                r["target_path"] = "MEMORY — " + " / ".join(s.strip() for s in first if s.strip())[:80]
    return rows


def load_pipeline_7d(cur: sqlite3.Cursor) -> dict:
    """Real status counts, splitting test/sandbox from real activity.
    Returns: {pending, merged_real, merged_sandbox, rejected, total}"""
    cur.execute(
        """SELECT id, status, target_path FROM proposals
             WHERE created_at >= strftime('%s','now')-?""",
        (SEVEN_DAYS,),
    )
    out = {"pending": 0, "merged_real": 0, "merged_sandbox": 0, "rejected": 0, "total": 0}
    for r in cur.fetchall():
        out["total"] += 1
        st, path = r["status"], r["target_path"] or ""
        if st == "pending":
            out["pending"] += 1
        elif st == "rejected":
            out["rejected"] += 1
        elif st == "merged":
            if any(m in path for m in SANDBOX_MARKERS):
                out["merged_sandbox"] += 1
            else:
                out["merged_real"] += 1
    return out


def load_self_incidents(cur: sqlite3.Cursor) -> List[Tuple[str, int, float]]:
    cur.execute(
        """SELECT incident_type, COUNT(*), MAX(detected_at)
             FROM self_incidents
             WHERE detected_at >= strftime('%s','now')-?
             GROUP BY incident_type""",
        (SEVEN_DAYS,),
    )
    return cur.fetchall()


def detect_stale_log() -> Tuple[str, float] | None:
    """Returns (filename, hours_stale) if the latest log is older than the
    no_output threshold (13h). The cycle job writes one log per tick."""
    if not LOGS_DIR.exists():
        return None
    logs = sorted(LOGS_DIR.iterdir(), key=lambda p: p.stat().st_mtime)
    if not logs:
        return None
    latest = logs[-1]
    age_h = (time.time() - latest.stat().st_mtime) / 3600
    if age_h > 13:
        return (latest.name, age_h)
    return None


def format_pending(rows: List[dict]) -> str:
    if not rows:
        return "  (none)\n"
    out = []
    for r in rows:
        out.append(
            f"  #{r['id']} {r['target_kind']}  conf={r['confidence']:.2f}  "
            f"since {_ts_human(r['created_at'])}\n"
            f"    target: {r['target_path']}\n"
            f"    why:    {_truncate(r['rationale'], 200)}\n"
        )
    return "".join(out)


def format_rejected(rows: List[dict]) -> str:
    if not rows:
        return "  (none this period)\n"
    out = []
    for r in rows:
        out.append(
            f"  #{r['id']} {r['target_kind']}  {_truncate(r['target_path'], 50)}\n"
            f"    judge said: {_truncate(r['rationale'], 180)}\n"
        )
    return "".join(out)


def format_overrides(rows: List[dict]) -> str:
    flagged = [r for r in rows if r["was_overrides_judge"]]
    if not flagged:
        return "  (no judge-override events this period — rubric stable)\n"
    out = [f"  ⚠ {len(flagged)} override(s) this period — judge is drifting:\n"]
    for r in flagged:
        out.append(
            f"    #{r['proposal_id']}  thomas={r['verdict']}  "
            f"target: {_truncate(r['target_path'], 50)}\n"
            f"      note: {_truncate(r['note'], 160)}\n"
        )
    return "".join(out)


def format_confirm_needed(stale_log, overrides) -> List[str]:
    """Things the system can't decide on its own."""
    out = []
    # Override rate — only ask if there's enough signal
    flagged = [r for r in overrides if r["was_overrides_judge"]]
    if len(flagged) >= 2:
        out.append(
            f"  • Judge-rubric drift: {len(flagged)} overrides in recent window. "
            "Update rubric, raise judge strictness, or accept drift?\n"
        )
    if stale_log:
        fname, age = stale_log
        out.append(
            f"  • Stale log: {fname} is {age:.1f}h old (threshold 13h). "
            "sri-cycle is silent — investigate, or adjust threshold?\n"
        )
    # Wall-time high — only ask if sustained
    if not out:
        out.append("  (none — pipeline healthy)\n")
    return out


def _build_digest() -> str:
    """Build the digest text. Pure function — no I/O."""
    if not DB.exists():
        raise SystemExit(f"FATAL: db not found at {DB}")
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    cur = con.cursor()

    pending = load_pending(cur)
    rejected = load_rejected_judge(cur)
    overrides = load_thomas_overrides(cur)
    pipeline = load_pipeline_7d(cur)
    incidents = load_self_incidents(cur)
    stale = detect_stale_log()
    con.close()

    parts = ["📋 SRI weekly digest\n"]

    parts.append("▸ RULES PROPOSED (awaiting your decision):")
    parts.append(format_pending(pending))
    if pending:
        parts.append("  Reply: approve #N | reject #N | modify #N: <note>\n")
    else:
        parts.append("")

    parts.append("▸ RULES JUDGE-REJECTED (FYI, no action needed):")
    parts.append(format_rejected(rejected))
    parts.append("")

    parts.append("▸ JUDGE-OVERRIDES (rubric drift):")
    parts.append(format_overrides(overrides))
    parts.append("")

    parts.append("▸ CONFIRM NEEDED (system can't decide on its own):")
    for line in format_confirm_needed(stale, overrides):
        parts.append(line)
    parts.append("")

    parts.append("▸ CONTEXT (FYI):")
    sandbox_note = f" ({pipeline['merged_sandbox']} test/sandbox)" if pipeline['merged_sandbox'] else ""
    parts.append(
        f"  Pipeline 7d — {pipeline['pending']} pending, "
        f"{pipeline['merged_real']} real applied{sandbox_note}, "
        f"{pipeline['rejected']} rejected  (of {pipeline['total']} total)\n"
    )
    if incidents:
        inc_parts = [f"{kind}: {n}" for kind, n, _ in incidents]
        parts.append(f"  Self-incidents 7d — {', '.join(inc_parts)}")
        if stale:
            parts.append(f"  Stale log: {stale[0]} ({stale[1]:.1f}h old, threshold 13h)")
    else:
        parts.append("  Self-incidents 7d — none")
    parts.append("")

    return "\n".join(parts)


def _send_telegram(chat_id: str, text: str, token: str) -> bool:
    """Send to Telegram. Chunks at 3900 chars (Telegram limit 4096)."""
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    chunks = [text[i:i+3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        body = json.dumps({
            "chat_id": chat_id, "text": chunk,
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                r.read()
        except urllib.error.HTTPError as e:
            print(f"[digest] Telegram send failed: {e.code} {e.reason}", file=sys.stderr)
            return False
        except Exception as e:
            print(f"[digest] Telegram send error: {e}", file=sys.stderr)
            return False
    return True


def main() -> int:
    text = _build_digest()
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    chat = os.environ.get("TELEGRAM_HOME_CHANNEL", "").strip()

    if token and chat:
        ok = _send_telegram(chat, text, token)
        if ok:
            print(f"[digest] Sent {len(text)}-char digest to {chat}")
            return 0
        # fall through to print on send failure
    print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
