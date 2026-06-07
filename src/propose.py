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
import incident_watcher
from rubric import RUBRIC_CURRENT


PROPOSER_PROMPT = Path(__file__).parent.parent / "prompts" / "proposer.md"
PROPOSER_MODEL = "MiniMax-M3"  # The main model proposes
API_URL = "https://api.minimax.io/v1/chat/completions"


def _call_m3(system: str, user: str, max_tokens: int = 4000) -> dict:
    """Returns {'content': str, 'input_tokens': int|None, 'output_tokens': int|None, 'ms': int}.
    Token counts come from the API response's `usage` field; missing/None
    if the API doesn't return them (e.g. older models or stream mode).
    """
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
    t0 = time.monotonic()
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    elapsed_ms = int((time.monotonic() - t0) * 1000)
    usage = data.get("usage") or {}
    return {
        "content": data["choices"][0]["message"]["content"],
        "input_tokens": usage.get("prompt_tokens"),
        "output_tokens": usage.get("completion_tokens"),
        "ms": elapsed_ms,
    }


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

    # Phase 2: stale memory candidates. Tell the proposer what existing
    # memory entries look stale — it can propose a removal (with
    # explicit thomas approval) or a refresh with new evidence.
    stale = db.list_stale_candidates(limit=10)
    if stale:
        system += "\n\n## Stale memory candidates (Phase 2)\n\n"
        system += (
            "The following memory entries look stale — either the source "
            "session is gone or they haven't been surfaced in 30+ days. "
            "If you have strong new evidence they're still correct, ignore. "
            "Otherwise you may propose:\n"
            "  - skill_patch / memory_add to refresh them with new evidence\n"
            "  - a clearly-marked removal proposal (thomas will be asked)\n\n"
        )
        for s in stale:
            content = (s.get("content") or "").strip()[:300]
            system += f"- lesson #{s['lesson_id']} ({s['reason']}): {content}\n"

    # Phase 3: pipeline gaps. Recent errors in the loop's own machinery
    # (API timeouts, parse failures, apply failures) are captured as
    # lessons. Surface them so the proposer can suggest fixes.
    recent_gaps = db.recent_gap_lessons(days=7, limit=5)
    if recent_gaps:
        system += "\n\n## Pipeline gaps from last 7 days (Phase 3)\n\n"
        system += (
            "The loop's own machinery has been failing in these ways. "
            "If you can identify a config or skill change that would "
            "prevent this class of failure, propose it. Do NOT propose "
            "changes to fix individual one-off errors.\n\n"
        )
        for g in recent_gaps:
            content = (g.get("content") or "").strip()[:300]
            system += f"- {content}\n"

    # Phase 4: self-referential incident. If a self_incident row exists
    # for the loop's own cron jobs, synthesize a "session" the proposer
    # can mine. We use the sessions_mined table to dedup — once an
    # incident is fed in, it's marked as 'incident_<id>' and skipped
    # thereafter.
    incident_session = incident_watcher.unmined_incident_as_session()
    if incident_session:
        synthetic_sid = f"incident_{incident_session['id']}"
        # Mark as mined so we don't re-feed
        db.mark_session_mined(synthetic_sid, proposals=0)
        system += "\n\n## Self-referential incident (Phase 4)\n\n"
        system += (
            "A cron job for this very loop failed. The next propose cycle "
            "should treat this as a session and propose a fix.\n\n"
            f"### Incident #{incident_session['id']}: {incident_session['incident_type']}\n"
            f"job: {incident_session['job_name']}\n"
            f"detail: {incident_session['detail']}\n\n"
            f"```\n{incident_session.get('last_log_lines', '')[-2000:]}\n```\n"
        )

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
            api_result = _call_m3(system, user_prompt)
        except Exception as e:
            print(f"    ! m3 call failed: {e}")
            # Phase 3: feed the gap back into the loop. The next proposer
            # cycle will see it via lessons_learned and can propose a fix
            # (e.g. shorter transcripts, JSON-mode prompt, smaller context).
            db.add_lesson(
                category="gap",
                content=f"propose API call failed in session {sid}: {type(e).__name__}: {str(e)[:200]}",
                source=sid,
            )
            continue

        raw = api_result["content"]
        proposals = _parse_proposals(raw)

        # Phase 3: parse failures. If 0 valid proposals AND we got raw
        # text back, the model emitted something we couldn't parse —
        # that's a real signal worth capturing.
        if raw and raw.strip() and not proposals:
            db.add_lesson(
                category="gap",
                content=(
                    f"propose parser rejected all output from session {sid}. "
                    f"First 200 chars: {raw[:200]}"
                ),
                source=sid,
            )

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
                    propose_ms=api_result["ms"],
                    input_tokens=api_result["input_tokens"],
                    output_tokens=api_result["output_tokens"],
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
