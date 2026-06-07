"""
propose.py — cron entry point for the m3 proposer agent.

This script doesn't LLM-call itself. Instead it assembles a self-contained
prompt bundle that gets fed to an m3 cron job. The cron job returns JSONL
of proposals, which we parse and persist.

Alternative: this script can be run as a pure script with the no_agent=True
flag in the cron job, where m3 is invoked via a one-shot OpenAI-compatible
call directly. We do the latter for cost + speed.
"""

import os
import json
import time
import urllib.request
import urllib.error
import argparse
from pathlib import Path

import db
import miner
from rubric import RUBRIC_CURRENT


PROPOSER_PROMPT = Path(__file__).parent.parent / "prompts" / "proposer.md"
PROPOSER_MODEL = "MiniMax-M3"  # The main model proposes
API_URL = "https://api.minimax.io/v1/chat/completions"


def _call_m3(system: str, user: str, max_tokens: int = 4000) -> str:
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY not set")
    body = json.dumps({
        "model": PROPOSER_MODEL,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "max_tokens": max_tokens,
        "temperature": 0.4,
    }).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def _parse_proposals(raw: str) -> list:
    """Parse the strict-JSONL output. Tolerate minor wrapping and
    embedded <think>...</think> reasoning blocks (m3 emits these)."""
    raw = raw.strip()
    # Strip <think>...</think> blocks (m3 reasoning artifacts)
    import re
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = raw.strip()
    # Strip ``` fences
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
    out = []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
            out.append(obj)
        except json.JSONDecodeError:
            # Try to find a JSON object in the line
            start = line.find("{")
            end = line.rfind("}")
            if start >= 0 and end > start:
                try:
                    out.append(json.loads(line[start:end+1]))
                except json.JSONDecodeError:
                    pass
    return out


def run(max_sessions: int = 3, dry_run: bool = False) -> dict:
    """Main entry: mine sessions, propose, persist."""
    db.init_db()
    system = PROPOSER_PROMPT.read_text()
    system += f"\n\n## Active judge rubric (version {db.latest_rubric()['version']})\n\n{RUBRIC_CURRENT}\n"

    # Inject the avoid-list: recent rejections/overrides so the proposer
    # doesn't re-propose the same class of bad idea. The cron agent that
    # consumes our prompt is non-interactive, so we have to bake this in
    # at construction time.
    neg_patterns = db.recent_negative_patterns(limit=10)
    if neg_patterns:
        system += "\n\n## Patterns to AVOID (from recent thomas rejections)\n\n"
        system += (
            "Thomas has recently rejected the following kinds of proposals. "
            "Do NOT propose changes in the same shape unless you have strong "
            "new evidence that the situation has changed. A 2-line list:\n\n"
        )
        for np in neg_patterns:
            tag = "[override]" if np["was_overrides_judge"] else "[reject]"
            path = np.get("target_path") or "(no path)"
            reason = (np.get("reason") or "").strip()[:300]
            system += f"- {tag} {np['target_kind']} {path}: {reason}\n"
        db.mark_neg_patterns_used([np["id"] for np in neg_patterns])

    sessions = miner.unmined_sessions(limit=max_sessions)
    if not sessions:
        print("[propose] No unmined sessions.")
        return {"proposed": 0, "mined": 0}

    print(f"[propose] Mining {len(sessions)} sessions with {PROPOSER_MODEL}...")
    total_proposed = 0
    for sid, path, msg_count in sessions:
        print(f"  - {sid} ({msg_count} msgs)")
        _, _, messages = miner.load_session(path)
        transcript = miner.format_for_proposer(sid, msg_count, messages)
        user_prompt = f"Analyze this session and emit proposals:\n\n{transcript}"

        try:
            raw = _call_m3(system, user_prompt)
        except Exception as e:
            print(f"    ! m3 call failed: {e}")
            continue

        proposals = _parse_proposals(raw)
        # Filter empty/no_proposals responses
        valid = []
        for p in proposals:
            if p.get("no_proposals"):
                print(f"    → no proposals: {p.get('reason', '?')[:80]}")
                continue
            if not p.get("target_kind") or not p.get("diff"):
                continue
            valid.append(p)

        print(f"    → {len(valid)} valid proposal(s)")
        total_proposed += len(valid)

        if dry_run:
            for p in valid:
                print(f"      [dry-run] {p['target_kind']} → {p.get('target_path', '?')}")
        else:
            for p in valid:
                pid = db.add_proposal(
                    target_kind=p["target_kind"],
                    diff=p["diff"],
                    rationale=p.get("rationale", ""),
                    evidence=p.get("evidence", ""),
                    target_path=p.get("target_path"),
                    source_session_id=sid,
                    confidence=float(p.get("confidence", 0.5)),
                )
                # Record lesson if provided
                if isinstance(p.get("lesson"), dict):
                    lesson = p["lesson"]
                    db.add_lesson(
                        category=lesson.get("category", "lesson"),
                        content=lesson.get("content", ""),
                        source=sid,
                    )
                print(f"      → proposal #{pid} ({p['target_kind']})")

        db.mark_session_mined(sid, proposals=len(valid))

    return {"proposed": total_proposed, "mined": len(sessions)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--max", type=int, default=3)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    result = run(max_sessions=args.max, dry_run=args.dry_run)
    print(json.dumps(result))
