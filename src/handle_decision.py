#!/usr/bin/env python3
"""
handle_decision.py — poll Telegram for callback_query taps on digest buttons.

callback_data format: "sri:approve:<id>" | "sri:reject:<id>" | "sri:modify:<id>"

On each tap:
  1. answerCallbackQuery so the toast goes away
  2. mark the proposal: approve -> 'merged' (apply.py will pick it up)
                          reject  -> 'rejected' (terminal)
                          modify  -> 'rejected' with a "modify requested" note
                                     (we can't do free-form text via buttons, so
                                     the modify button just signals intent and
                                     tells thomas to reply with the note)
  3. edit the digest message to show the decision inline (✅/❌/✏️)
  4. acknowledge back to thomas with a short status line

State management for polling offset is persisted in data/decision_state.json
so a restart picks up where we left off. Approved proposals transition
to 'merged' status — apply.py (cron sri-apply-merged) actually writes to
the files. This script only records decisions.

Tested in isolation with a fake getUpdates response. For end-to-end
verification, use the test mode:
  python3 src/handle_decision.py --once --dry-run
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
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DB = ROOT / "data" / "loop.db"
STATE_FILE = ROOT / "data" / "decision_state.json"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"

ALLOWED_USERS: list[int] = []  # populated lazily from env in main()

# 60s long-poll is the sweet spot — Telegram docs say <= 30s for production,
# but local dev 5-10s is fine. We use 5s here to keep cron wakeups snappy.
POLL_TIMEOUT = 5


def _load_allowed_users() -> list[int]:
    """Lazy re-read of TELEGRAM_ALLOWED_USERS from env. Called at the top of
    every _process_callback so a runtime change is picked up without restart."""
    raw = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


# ──────────────────────── DB helpers ────────────────────────

def _conn() -> sqlite3.Connection:
    con = sqlite3.connect(DB)
    con.row_factory = sqlite3.Row
    return con


def _mark_merged(proposal_id: int) -> dict | None:
    """Set status='merged'. Returns the proposal row, or None if not pending."""
    con = _conn()
    row = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row:
        con.close()
        return None
    if row["status"] != "pending":
        con.close()
        return dict(row)  # already decided, no-op
    con.execute(
        "UPDATE proposals SET status='merged', merged_at=? WHERE id=?",
        (time.time(), proposal_id),
    )
    con.commit()
    updated = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    con.close()
    return dict(updated)


def _mark_rejected(proposal_id: int, note: str = "") -> dict | None:
    con = _conn()
    row = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    if not row:
        con.close()
        return None
    if row["status"] != "pending":
        con.close()
        return dict(row)
    con.execute("UPDATE proposals SET status='rejected' WHERE id=?", (proposal_id,))
    con.execute(
        """INSERT INTO thomas_feedback
             (proposal_id, feedback_at, verdict, note, was_overrides_judge)
           VALUES (?, ?, ?, ?, ?)""",
        (proposal_id, time.time(), "reject",
         note or "Rejected via Telegram inline button", 0),
    )
    con.commit()
    updated = con.execute("SELECT * FROM proposals WHERE id=?", (proposal_id,)).fetchone()
    con.close()
    return dict(updated)


def _record_override_feedback(proposal_id: int, verdict: str, note: str) -> None:
    """For approve/reject — log thomas_feedback so override stats stay accurate."""
    con = _conn()
    con.execute(
        """INSERT INTO thomas_feedback
             (proposal_id, feedback_at, verdict, note, was_overrides_judge)
           VALUES (?, ?, ?, ?, ?)""",
        (proposal_id, time.time(), verdict, note, 0),
    )
    con.commit()
    con.close()


# ──────────────────────── Telegram API ────────────────────────

def _api(token: str, method: str, params: dict | None = None) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    data = json.dumps(params or {}).encode()
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=POLL_TIMEOUT + 5) as r:
            return json.loads(r.read())
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as e:
        print(f"[handle_decision] API {method} error: {e}", file=sys.stderr)
        return {"ok": False, "error": str(e)}


def _answer_callback(token: str, callback_query_id: str, text: str) -> None:
    _api(token, "answerCallbackQuery", {
        "callback_query_id": callback_query_id,
        "text": text,
        "show_alert": False,
    })


def _edit_message(token: str, chat_id: str | int, message_id: int,
                  new_text: str) -> None:
    _api(token, "editMessageText", {
        "chat_id": chat_id,
        "message_id": message_id,
        "text": new_text,
    })


# ──────────────────────── Polling state ────────────────────────

def _load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            pass
    return {"offset": 0, "handled_ids": []}


def _save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(json.dumps(state, indent=2))


# ──────────────────────── Decision logic ────────────────────────

def _process_callback(query: dict, token: str, dry_run: bool) -> str:
    """Returns a one-line status string."""
    qid = query.get("id", "")
    data = (query.get("data") or "").strip()
    from_user = query.get("from", {}).get("id")
    msg = query.get("message") or {}
    chat_id = msg.get("chat", {}).get("id")
    message_id = msg.get("message_id")
    original_text = msg.get("text") or msg.get("caption") or ""

    if from_user not in _load_allowed_users():
        _answer_callback(token, qid, "🚫 not authorized")
        return f"rejected callback from non-allowed user {from_user}"

    parts = data.split(":")
    if len(parts) != 3 or parts[0] != "sri":
        _answer_callback(token, qid, "unknown action")
        return f"unknown callback_data: {data!r}"
    action, pid_str = parts[1], parts[2]
    try:
        proposal_id = int(pid_str)
    except ValueError:
        _answer_callback(token, qid, "bad id")
        return f"bad proposal id in {data!r}"

    if action == "approve":
        if dry_run:
            _answer_callback(token, qid, f"DRY-RUN: would approve #{proposal_id}")
            return f"DRY-RUN approved #{proposal_id}"
        updated = _mark_merged(proposal_id)
        if updated is None:
            _answer_callback(token, qid, f"#{proposal_id} not found")
            return f"approve: #{proposal_id} not found"
        if updated["status"] != "merged":
            _answer_callback(token, qid, f"#{proposal_id} already {updated['status']}")
            return f"approve: #{proposal_id} already {updated['status']}"
        _record_override_feedback(
            proposal_id, "approve",
            f"Approved via Telegram inline button (chat {chat_id}, msg {message_id})",
        )
        # Edit digest message — append ✅ marker to the proposal line
        new_text = _stamp_decision(original_text, proposal_id, "✅ APPROVED — apply.py will pick it up")
        if message_id:
            _edit_message(token, chat_id, message_id, new_text)
        _answer_callback(token, qid, f"✅ approved #{proposal_id}")
        return f"approved #{proposal_id}"

    elif action == "reject":
        if dry_run:
            _answer_callback(token, qid, f"DRY-RUN: would reject #{proposal_id}")
            return f"DRY-RUN rejected #{proposal_id}"
        updated = _mark_rejected(proposal_id, "Rejected via Telegram inline button")
        if updated is None:
            _answer_callback(token, qid, f"#{proposal_id} not found")
            return f"reject: #{proposal_id} not found"
        if updated["status"] != "rejected":
            _answer_callback(token, qid, f"#{proposal_id} already {updated['status']}")
            return f"reject: #{proposal_id} already {updated['status']}"
        new_text = _stamp_decision(original_text, proposal_id, "❌ REJECTED")
        if message_id:
            _edit_message(token, chat_id, message_id, new_text)
        _answer_callback(token, qid, f"❌ rejected #{proposal_id}")
        return f"rejected #{proposal_id}"

    elif action == "modify":
        # Modify needs a free-form note we can't get from a button. Tap signals
        # intent; thomas still replies with the note. Mark the proposal as
        # "rejected" with a placeholder — the next propose cycle will surface
        # the request as a new proposal. Alternative: leave status='pending'
        # and just acknowledge. Leaving pending is cleaner: the human note
        # arriving later can still approve/reject, and the original proposal
        # stays visible in the digest until thomas replies with the note.
        _answer_callback(
            token, qid,
            f"✏️ noted — reply with your note for #{proposal_id} (e.g. 'modify #{proposal_id}: <your note>')",
        )
        return f"modify intent for #{proposal_id} (waiting for note)"

    else:
        _answer_callback(token, qid, "unknown action")
        return f"unknown action {action!r}"


def _stamp_decision(text: str, proposal_id: int, stamp: str) -> str:
    """Append a decision line to the digest message text. Best-effort;
    if the proposal line is found, annotate it; otherwise just append."""
    marker = f"#{proposal_id} "
    new_lines = []
    stamped = False
    for line in text.splitlines():
        if marker in line and stamp not in line:
            new_lines.append(f"{line}  —  {stamp}")
            stamped = True
        else:
            new_lines.append(line)
    if not stamped:
        new_lines.append(f"\n{stamp}  (proposal #{proposal_id})")
    return "\n".join(new_lines)


# ──────────────────────── Main loop ────────────────────────

def _poll_once(token: str, dry_run: bool) -> list[str]:
    state = _load_state()
    resp = _api(token, "getUpdates", {
        "offset": state["offset"],
        "timeout": POLL_TIMEOUT,
        "allowed_updates": ["callback_query"],
    })
    if not resp.get("ok"):
        return []
    results = []
    handled = set(state.get("handled_ids", []))
    for upd in resp.get("result", []):
        state["offset"] = max(state["offset"], upd["update_id"] + 1)
        cb = upd.get("callback_query")
        if not cb:
            continue
        if cb["id"] in handled:
            continue
        handled.add(cb["id"])
        result = _process_callback(cb, token, dry_run=dry_run)
        results.append(result)
    # Cap handled_ids history to last 1000
    state["handled_ids"] = list(handled)[-1000:]
    _save_state(state)
    return results


def main() -> int:
    token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token:
        print("[handle_decision] TELEGRAM_BOT_TOKEN not set", file=sys.stderr)
        return 1
    allowed = os.environ.get("TELEGRAM_ALLOWED_USERS", "").strip()
    if not allowed:
        print("[handle_decision] TELEGRAM_ALLOWED_USERS not set", file=sys.stderr)
        return 1
    global ALLOWED_USERS
    ALLOWED_USERS = [int(x) for x in allowed.split(",") if x.strip().isdigit()]

    dry_run = "--dry-run" in sys.argv
    once = "--once" in sys.argv

    if once:
        results = _poll_once(token, dry_run=dry_run)
        for r in results:
            print(r)
        if not results:
            print("[handle_decision] no callbacks")
        return 0

    # Continuous mode — used when run as a foreground process. Cron wraps
    # the same script in --once mode every 1-2 minutes for reliability.
    print(f"[handle_decision] polling for callbacks (allowed={ALLOWED_USERS})")
    while True:
        try:
            results = _poll_once(token, dry_run=dry_run)
            for r in results:
                print(r)
        except KeyboardInterrupt:
            return 0
        except Exception as e:
            print(f"[handle_decision] poll error: {e}", file=sys.stderr)
            time.sleep(2)


if __name__ == "__main__":
    sys.exit(main())
