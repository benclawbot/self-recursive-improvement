"""
self_improve.py — the recursive part. Eval the judge's calibration and
propose rubric updates when override rate is high.

This is the *one* thing in the loop that modifies its own rules. So it:
  - Never auto-merges rubric changes (always queues for thomas)
  - Computes override rate on the last 20 judged proposals
  - Asks m3 to propose a new rubric version, asking the judge to
    self-critique the current rubric
  - Records the proposal as 'rubric_update' so thomas can review

This is run weekly, after the digest, by a separate cron.
"""

import os
import json
import time
import urllib.request
import argparse
from pathlib import Path

import db


API_URL = "https://api.minimax.io/v1/chat/completions"
MODEL = "MiniMax-M3"


def _call_m3(prompt: str, max_tokens: int = 3000) -> str:
    api_key = os.environ.get("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY not set")
    body = json.dumps({
        "model": MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode()
    req = urllib.request.Request(
        API_URL, data=body,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=120) as resp:
        data = json.loads(resp.read())
    return data["choices"][0]["message"]["content"]


def get_recent_overrides(limit: int = 20) -> list:
    """Pull the last N overrides (judge said X, thomas said Y) for analysis."""
    with db.conn() as c:
        rows = c.execute(
            """SELECT p.id, p.target_kind, p.target_path, p.rationale, p.evidence,
                      j.verdict AS judge_verdict, j.reasoning AS judge_reasoning,
                      t.verdict AS thomas_verdict, t.note AS thomas_note
               FROM thomas_feedback t
               JOIN proposals p ON p.id = t.proposal_id
               LEFT JOIN judge_verdicts j ON j.id = (
                   SELECT id FROM judge_verdicts WHERE proposal_id = p.id ORDER BY id DESC LIMIT 1
               )
               WHERE t.was_overrides_judge = 1
               ORDER BY t.feedback_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def get_recent_agreements(limit: int = 20) -> list:
    """Cases where judge and thomas agreed — for negative signal too."""
    with db.conn() as c:
        rows = c.execute(
            """SELECT p.id, p.target_kind, j.verdict AS judge_verdict, t.verdict AS thomas_verdict
               FROM thomas_feedback t
               JOIN proposals p ON p.id = t.proposal_id
               LEFT JOIN judge_verdicts j ON j.id = (
                   SELECT id FROM judge_verdicts WHERE proposal_id = p.id ORDER BY id DESC LIMIT 1
               )
               WHERE t.was_overrides_judge = 0
               ORDER BY t.feedback_at DESC LIMIT ?""",
            (limit,),
        ).fetchall()
    return [dict(r) for r in rows]


def propose_rubric_update(overrides: list, agreements: list,
                          current_rubric: dict) -> dict | None:
    """Ask m3 to propose a rubric refinement based on override patterns.
    Returns the new rubric prompt text, or None if no change recommended."""
    if len(overrides) < 3:
        print(f"[self-improve] Only {len(overrides)} overrides — need at least 3 to refine")
        return None

    override_rate = len(overrides) / max(1, len(overrides) + len(agreements))
    print(f"[self-improve] {len(overrides)} overrides / {len(agreements)+len(overrides)} decisions "
          f"= {override_rate*100:.0f}% override rate")

    overrides_text = "\n\n".join(
        f"### Override #{o['id']}\n"
        f"- target: {o['target_kind']} {o.get('target_path','')}\n"
        f"- judge said: {o.get('judge_verdict','?')} — {o.get('judge_reasoning','?')[:300]}\n"
        f"- thomas said: {o.get('thomas_verdict','?')} — {o.get('thomas_note','(no note)')[:300]}"
        for o in overrides
    )

    prompt = f"""\
You are improving a judge rubric for an AI self-improvement loop.

The current rubric (version {current_rubric['version']}) has a
{override_rate*100:.0f}% override rate — meaning thomas reversed m2.7's
verdict that often. Your job: propose a refined rubric that better
predicts thomas's decisions.

## Current rubric

{current_rubric['prompt_text']}

## Recent overrides (judge said X, thomas said Y)

{overrides_text}

## What to do

Look for PATTERNS across the overrides. Are the same criteria tripping
the judge up? Is the rubric too strict on one axis and too loose on
another? Are there rules thomas clearly cares about that the rubric
doesn't capture?

Then propose a REFINED rubric. The structure can stay the same — adjust
the criteria, add missing ones, sharpen the calibration guidance. The
output is the FULL new rubric prompt (not a diff).

If the rubric looks fine and overrides are random noise, say so and
emit `{{"no_change_needed": true, "reasoning": "..."}}`.

Output: ONLY the new rubric prompt text OR the no_change JSON, no preamble.
"""

    try:
        new_rubric = _call_m3(prompt, max_tokens=2000)
    except Exception as e:
        print(f"[self-improve] m3 call failed: {e}")
        return None

    new_rubric = new_rubric.strip()
    if '"no_change_needed": true' in new_rubric or new_rubric.startswith("{"):
        print(f"[self-improve] m3 says no change needed")
        return None

    return {
        "prompt_text": new_rubric,
        "parent_version": current_rubric["version"],
        "notes": f"Auto-proposed after {len(overrides)} overrides / {override_rate*100:.0f}% rate",
    }


def run(dry_run: bool = False):
    db.init_db()
    overrides = get_recent_overrides(limit=20)
    agreements = get_recent_agreements(limit=20)
    current = db.latest_rubric()
    if not current:
        print("[self-improve] No rubric found.")
        return

    proposal = propose_rubric_update(overrides, agreements, current)
    if not proposal:
        return

    if dry_run:
        print("[self-improve] DRY RUN — would propose new rubric:")
        print(proposal["prompt_text"][:500])
        return

    # Persist as a rubric_update proposal — thomas reviews it
    pid = db.add_proposal(
        target_kind="rubric_update",
        diff=proposal["prompt_text"],
        rationale="Refined rubric after override-rate analysis",
        evidence=f"Overrides: {len(overrides)}/{len(overrides)+len(agreements)} "
                 f"({100*len(overrides)/max(1,len(overrides)+len(agreements)):.0f}%)",
        target_path=None,
        source_session_id="self_improve_loop",
        confidence=0.6,
    )
    # Also save the rubric version so version tracking is correct when approved
    # (apply step will swap active rubric in)
    print(f"[self-improve] Proposed rubric v{current['version']+1} as proposal #{pid}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    run(dry_run=args.dry_run)
