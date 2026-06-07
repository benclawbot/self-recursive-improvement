"""
Judge — calls m2.7 against the active rubric to evaluate proposals.

This is a strict second-opinion reviewer. The m3 proposer generates
candidates, m2.7 judges them, thomas has final say. The m2.7 verdict
gets recorded with its reasoning so we can later evaluate whether
the rubric is calibrated (override rate = judge failure rate).
"""

import os
import json
import time
import urllib.request
import urllib.error
from typing import Optional

from db import latest_rubric, add_judge_verdict
from rubric import RUBRIC_CURRENT


JUDGE_MODEL = "MiniMax-M2.7"
API_URL = "https://api.minimax.io/v1/chat/completions"

def _call_m27(prompt: str, max_tokens: int = 600) -> dict:
    """Returns {'content': str, 'input_tokens': int|None, 'output_tokens': int|None, 'ms': int}.
    Same shape as propose._call_m3 — kept consistent so cycle_stats
    can sum both call sites uniformly.
    """
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY not set")

    body = json.dumps({
        "model": JUDGE_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": 1500,
        "temperature": 0.2,  # low temp for consistent judging
    }).encode()

    req = urllib.request.Request(
        API_URL,
        data=body,
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


def _parse_verdict(raw: str) -> dict:
    """The judge is asked to emit strict JSON. We tolerate mild wrapping
    AND <think>...</think> blocks (m2.7 emits these too)."""
    import re
    raw = raw.strip()
    # Strip <think>...</think> blocks (both m2.7 and m3 emit these)
    raw = re.sub(r"<think>.*?</think>", "", raw, flags=re.DOTALL)
    raw = raw.strip()
    # Strip ```json fences if present
    if raw.startswith("```"):
        raw = raw.split("```", 2)[1]
        if raw.startswith("json"):
            raw = raw[4:]
        raw = raw.strip().rstrip("`").strip()
    try:
        v = json.loads(raw)
        return {
            "verdict": v.get("verdict", "reject"),
            "score": float(v.get("score", 0.0)),
            "reasoning": v.get("reasoning", ""),
        }
    except (json.JSONDecodeError, ValueError):
        return {
            "verdict": "reject",
            "score": 0.0,
            "reasoning": f"[judge output unparseable: {raw[:200]}]",
        }


def judge_proposal(proposal: dict) -> dict:
    """Judge a single proposal. Returns the parsed verdict dict and
    also records it in the database."""
    rubric = latest_rubric()
    rubric_text = rubric["prompt_text"] if rubric else RUBRIC_CURRENT

    user_prompt = f"""\
RUBRIC (version {rubric['version'] if rubric else 1}):
{rubric_text}

---

PROPOSAL TO JUDGE:

target_kind: {proposal['target_kind']}
target_path: {proposal.get('target_path', '(none)')}
rationale: {proposal.get('rationale', '(none)')}
confidence: {proposal.get('confidence', 0.5)}
source_session_id: {proposal.get('source_session_id', '(unknown)')}

evidence:
{proposal.get('evidence', '(none provided)')}

diff:
{proposal.get('diff', '(none)')}

---

Respond with ONLY the JSON verdict, no preamble.
"""

    try:
        api_result = _call_m27(user_prompt)
    except Exception as call_exc:
        # Phase 3: write a gap lesson so the next proposer cycle sees
        # the failure pattern. Capped at one lesson per call — avoid
        # flooding if m2.7 is down for hours.
        try:
            db.add_lesson(
                category="gap",
                content=(
                    f"judge API call failed for proposal {proposal['id']} "
                    f"({proposal['target_kind']}): {type(call_exc).__name__}: "
                    f"{str(call_exc)[:200]}"
                ),
                source=f"judge:{proposal['id']}",
            )
        except Exception:
            pass  # Last-resort: don't let gap-feedback break judging
        # For now: surface the error, return a reject verdict, store it
        # with judge_error so digest can show "judge failures: N".
        add_judge_verdict(
            proposal_id=proposal["id"],
            judge_model=JUDGE_MODEL,
            verdict="reject",
            score=0.0,
            reasoning=f"[judge call failed: {call_exc}]",
            error=str(call_exc)[:500],
        )
        return {
            "verdict": "reject",
            "score": 0.0,
            "reasoning": f"[judge call failed: {call_exc}]",
        }

    raw = api_result["content"]
    verdict = _parse_verdict(raw)

    add_judge_verdict(
        proposal_id=proposal["id"],
        judge_model=JUDGE_MODEL,
        verdict=verdict["verdict"],
        score=verdict["score"],
        reasoning=verdict["reasoning"],
        judge_ms=api_result["ms"],
        input_tokens=api_result["input_tokens"],
        output_tokens=api_result["output_tokens"],
    )
    return verdict


def judge_batch(proposals: list, verbose: bool = True) -> list:
    """Judge a batch of proposals. Returns list of (proposal, verdict) tuples."""
    results = []
    for p in proposals:
        try:
            v = judge_proposal(p)
            if verbose:
                mark = {"approve": "✓", "reject": "✗", "needs_work": "?"}.get(v["verdict"], "?")
                print(f"  {mark} [{v['score']:.2f}] proposal #{p['id']} ({p['target_kind']}): {v['reasoning'][:80]}")
            results.append((p, v))
        except Exception as e:
            if verbose:
                print(f"  ! proposal #{p['id']} judge failed: {e}")
            results.append((p, {"verdict": "reject", "score": 0.0, "reasoning": f"judge error: {e}"}))
    return results


if __name__ == "__main__":
    import sys
    from db import pending_proposals

    proposals = pending_proposals()
    if not proposals:
        print("No pending proposals to judge.")
        sys.exit(0)
    print(f"Judging {len(proposals)} pending proposals with {JUDGE_MODEL}...")
    judge_batch(proposals)
    print("Done.")
