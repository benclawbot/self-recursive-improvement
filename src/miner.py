"""
Miner — extracts candidate patterns from recent hermes sessions.

Hermes stores sessions as JSONL files in ~/.hermes/sessions/
Each line is {role, content, timestamp}.

This module: fetches recent sessions, filters out mined ones, formats
them as transcripts for the proposer LLM.

The actual *interpretation* of sessions into proposals is done by the
proposer agent (m3) in a cron job. This module is plumbing.
"""

import json
import time
from pathlib import Path
from typing import Iterator

HERMES_SESSIONS = Path.home() / ".hermes" / "sessions"


def list_sessions(min_age_hours: float = 1.0, limit: int = 30) -> list:
    """Return session file paths newest-first, skipping very recent ones."""
    cutoff = time.time() - (min_age_hours * 3600)
    files = []
    for f in HERMES_SESSIONS.glob("*.jsonl"):
        # Filename: YYYYMMDD_HHMMSS_<id>.jsonl
        try:
            ts_part = f.stem.split("_")[0] + f.stem.split("_")[1]
            file_time = time.mktime(time.strptime(ts_part, "%Y%m%d%H%M%S"))
        except (ValueError, IndexError):
            file_time = f.stat().st_mtime
        if file_time < cutoff:
            files.append((file_time, f))
    files.sort(reverse=True)
    return [f for _, f in files[:limit]]


def session_id_from_path(p: Path) -> str:
    """Stable id from filename (no extension)."""
    return p.stem


def load_session(path: Path, max_msgs: int = 30) -> tuple:
    """Load a session file. Returns (session_id, msg_count, messages).

    Caps at 30 messages (head 15 + tail 15) to keep the proposer LLM
    call under the 60s urllib timeout. Anything bigger gets mined from
    the digest anyway; the patterns surface from the first 15 and the
    last 15, which is where the framing and outcome live.
    """
    sid = session_id_from_path(path)
    messages = []
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
                messages.append({
                    "role": msg.get("role", "?"),
                    "content": msg.get("content", ""),
                    "timestamp": msg.get("timestamp", ""),
                })
            except json.JSONDecodeError:
                continue
    # Truncate long sessions: head 15 + tail 15
    if len(messages) > max_msgs:
        head = messages[:15]
        tail = messages[-(max_msgs - 15):]
        omitted = len(messages) - max_msgs
        messages = head + [{
            "role": "system",
            "content": f"[... {omitted} messages omitted for brevity ...]",
            "timestamp": "",
        }] + tail
    return sid, len(messages), messages


def unmined_sessions(limit: int = 5, min_age_hours: float = 1.0,
                     min_messages: int = 6, max_messages: int = 200,
                     scan_window: int = 200) -> list:
    """Find sessions worth mining: unmined, old enough, big enough, not too big.

    `max_messages` filter exists because very long sessions (200+ msgs)
    are usually long implementation sessions whose patterns surface in
    the digest anyway. Mining them is more LLM cost than insight.

    `scan_window` controls how many recent session files we look at
    before giving up. With many already-mined or oversized sessions
    in the recent window, a small multiplier (`limit * 4`) used to miss
    eligible candidates entirely. 200 is a generous cap that still
    keeps the scan O(filenames-glob) cheap.
    """
    from db import was_session_mined
    out = []
    for path in list_sessions(min_age_hours=min_age_hours, limit=scan_window):
        sid = session_id_from_path(path)
        if was_session_mined(sid):
            continue
        # Quick msg count check
        try:
            with open(path) as f:
                msg_count = sum(1 for _ in f)
        except OSError:
            continue
        if msg_count < min_messages:
            continue
        if msg_count > max_messages:
            continue
        out.append((sid, path, msg_count))
        if len(out) >= limit:
            break
    return out


def format_for_proposer(sid: str, msg_count: int, messages: list,
                        source: str = "local") -> str:
    """Format as a transcript for the proposer prompt."""
    lines = [
        f"# Session: {sid}",
        f"id: {sid}",
        f"source: {source}",
        f"messages: {msg_count}",
        "",
        "## Transcript",
        "",
    ]
    for m in messages:
        ts = m.get("timestamp", "?")
        content = (m.get("content") or "")[:2000]
        # Strip excessive whitespace for prompt legibility
        content = "\n".join(line.rstrip() for line in content.split("\n")[:50])
        lines.append(f"### {m['role']} @ {ts}")
        lines.append(content)
        lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    sessions = unmined_sessions(limit=5)
    print(f"Found {len(sessions)} unmined sessions:")
    for sid, path, count in sessions:
        print(f"  - {sid} ({count} msgs)  {path.name}")
