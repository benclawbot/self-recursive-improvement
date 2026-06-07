"""
Digest — weekly Telegram summary of lessons learned and proposals.

Reads from lessons_learned + recent proposals + override stats.
Sends via Telegram bot to thomas's home channel.
"""

import os
import json
import urllib.request
import urllib.error
from datetime import datetime, timezone
from pathlib import Path

from db import (
    unsent_lessons, mark_lessons_sent, pending_proposals,
    override_stats, latest_rubric, outcome_stats,
    cycle_health_summary, recent_token_costs,
    stale_memory_stats, list_stale_candidates,
    recent_pipeline_errors,
    recent_incidents, incident_stats,
)
import branch


TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
HERMES_HOME = Path.home() / ".hermes"


def _send_telegram(chat_id: str, text: str) -> bool:
    """Send a message via Telegram Bot API. Returns True on success."""
    if not TELEGRAM_BOT_TOKEN:
        print("[digest] TELEGRAM_BOT_TOKEN not set, printing to stdout instead:")
        print(text)
        return False
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    # Telegram max message length is 4096 chars
    chunks = [text[i:i+3900] for i in range(0, len(text), 3900)] or [""]
    for chunk in chunks:
        body = json.dumps({
            "chat_id": chat_id,
            "text": chunk,
            "parse_mode": "MarkdownV2",
            "disable_web_page_preview": True,
        }).encode()
        req = urllib.request.Request(
            url, data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                resp.read()
        except urllib.error.HTTPError as e:
            # MarkdownV2 can be finicky; retry as plain text
            plain_body = json.dumps({
                "chat_id": chat_id,
                "text": chunk,
                "disable_web_page_preview": True,
            }).encode()
            req2 = urllib.request.Request(
                url, data=plain_body,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            with urllib.request.urlopen(req2, timeout=30) as resp2:
                resp2.read()
    return True


def get_target_chat() -> str:
    """Resolve Thomas's home chat id from env, then hermes config.

    Resolution order:
      1. TELEGRAM_HOME_CHANNEL env var (set in ~/.hermes/.env)
      2. home_channels[0] in config.yaml if it starts with "telegram:"
      3. platforms.telegram.home_chat_id in config.yaml
    """
    # 1. env var — the canonical home channel location
    env_chat = os.environ.get("TELEGRAM_HOME_CHANNEL", "").strip()
    if env_chat:
        return env_chat
    # 2. config.yaml home_channels
    cfg = HERMES_HOME / "config.yaml"
    if cfg.exists():
        try:
            import yaml
            with open(cfg) as f:
                data = yaml.safe_load(f) or {}
            for ch in data.get("home_channels", []) or []:
                if isinstance(ch, str) and ch.startswith("telegram:"):
                    return ch.split(":", 1)[1]
            plats = data.get("platforms", {})
            tg = plats.get("telegram", {})
            chat = tg.get("home_chat_id", "")
            if chat:
                return chat
        except Exception:
            pass
    return ""


def format_lesson(l: dict) -> str:
    cat_emoji = {
        "pattern": "🔁", "gap": "🕳", "lesson": "💡",
        "correction": "✏️",
    }.get(l["category"], "•")
    return f"{cat_emoji} *{l['category']}*: {l['content'][:400]}"


def build_weekly_digest() -> str:
    """Build the markdown for the weekly digest."""
    lessons = unsent_lessons(limit=30)
    pending = pending_proposals()
    stats = override_stats()
    rubric = latest_rubric()

    week = datetime.now(timezone.utc).strftime("%Y-%W")
    lines = [f"🧠 *Self-Improvement Digest — week {week}*", ""]

    # Stats first — at-a-glance health check
    if stats.get("total_judged", 0) > 0:
        rate = stats.get("override_rate", 0) or 0
        lines.append(f"📊 *Judge health*: {stats['total_judged']} proposals judged, "
                     f"{stats['overrides']} overrides ({rate*100:.0f}% override rate)")
    else:
        lines.append("📊 *Judge health*: no proposals judged yet")

    if rubric:
        lines.append(f"📜 *Active rubric*: v{rubric['version']}")
    lines.append("")

    # Phase 1: cycle health (wall time + token cost). Catches perf drift
    # weeks before thomas would notice manually.
    health = cycle_health_summary(limit=50)
    if health.get("count", 0) > 0:
        median_s = health["median_ms"] / 1000
        p95_s = health["p95_ms"] / 1000
        lines.append(f"⏱ *Cycle health* (last {health['count']}): "
                     f"median {median_s:.1f}s, p95 {p95_s:.1f}s "
                     f"(min {health['min_ms']/1000:.1f}s, max {health['max_ms']/1000:.1f}s)")
        tokens = recent_token_costs(limit=50)
        total_tokens = sum(tokens.values())
        if total_tokens > 0:
            # Rough cost: $3/1M input for m3, $15/1M output. Configurable
            # via env in future; hardcoded as a ballpark.
            in_cost = (tokens["propose_in"] + tokens["judge_in"]) / 1_000_000 * 1.5
            out_cost = (tokens["propose_out"] + tokens["judge_out"]) / 1_000_000 * 8
            total_cost = in_cost + out_cost
            lines.append(f"   tokens: {total_tokens:,} (~${total_cost:.2f} est.)")
    else:
        lines.append("⏱ *Cycle health*: no cycles recorded yet (Phase 1 just shipped)")
    lines.append("")

    # Outcome health: second signal — did the changes themselves help?
    # Defaulting to 'neutral' after 7 days means this takes a few weeks
    # to populate. Once we have ~10 graded outcomes, the ratio becomes
    # a real drift detector.
    out_stats = outcome_stats()
    total_graded = sum(out_stats.values())
    if total_graded > 0:
        helped = out_stats.get("helped", 0)
        neutral = out_stats.get("neutral", 0)
        reverted = out_stats.get("reverted", 0)
        recor = out_stats.get("recorrected", 0)
        positive = helped
        negative = reverted + recor
        lines.append(
            f"🧪 *Change outcomes*: {total_graded} graded — "
            f"✅{helped} helped · ➖{neutral} neutral · "
            f"❌{reverted} reverted · 🔧{recor} recor"
        )
        if positive + negative > 0:
            ratio = positive / (positive + negative)
            lines.append(f"   help ratio: {ratio*100:.0f}% ({positive}/{positive+negative})")
    else:
        lines.append("🧪 *Change outcomes*: no graded changes yet (need 7+ days)")
    lines.append("")

    # Lessons
    if lessons:
        lines.append(f"📚 *Lessons this week* ({len(lessons)}):")
        for l in lessons[:15]:
            lines.append(format_lesson(l))
        if len(lessons) > 15:
            lines.append(f"_... and {len(lessons) - 15} more_")
        lines.append("")

    # Pending proposals — things waiting for thomas's decision
    if pending:
        lines.append(f"⏳ *Pending your review* ({len(pending)}):")
        for p in pending[:10]:
            judge_v = p.get("judge_verdict", "?")
            kind = p.get("target_kind", "?")
            target = p.get("target_path", "")
            target_short = target.split("/")[-1] if target else ""
            lines.append(f"  • `#{p['id']}` {kind} {target_short} — judge: *{judge_v}*")
            rationale = (p.get("rationale") or "")[:200]
            if rationale:
                lines.append(f"    _{rationale}_")
        lines.append("")

    if not lessons and not pending:
        lines.append("_No new lessons or pending proposals this week. The loop is quiet — that's fine._")
    lines.append("")

    # Phase 3: pipeline errors (the loop's own machinery failing)
    errs = recent_pipeline_errors(days=7)
    if errs["total"] > 0:
        lines.append(f"⚠️ *Pipeline errors (7d)*: {errs['total']} — "
                     f"propose: {errs['propose']}, judge: {errs['judge']}, "
                     f"apply: {errs['apply']}")
    else:
        lines.append("✅ *Pipeline*: no errors in last 7d")
    lines.append("")

    # Phase 4: self-referential incidents
    inc_stats = incident_stats(days=7)
    total_inc = sum(inc_stats.values())
    if total_inc > 0:
        lines.append(f"🪞 *Self-incidents (7d)*: {total_inc} — "
                     + " · ".join(f"{k}: {v}" for k, v in sorted(inc_stats.items())))
        # Show the most recent one
        recent_inc = recent_incidents(days=7, limit=1)
        if recent_inc:
            inc = recent_inc[0]
            lines.append(f"   latest: {inc['job_name']} ({inc['incident_type']}) — {inc['detail'][:100]}")
    else:
        lines.append("🪞 *Self-incidents*: loop's own cron jobs all healthy (7d)")
    lines.append("")

    # Phase 2: memory hygiene
    mem_stats = stale_memory_stats()
    total_stale = sum(mem_stats.values())
    if total_stale > 0:
        lines.append(f"🧹 *Memory hygiene*: {total_stale} stale candidate(s) — "
                     f"📂{mem_stats.get('source_gone', 0)} source gone · "
                     f"⏳{mem_stats.get('unsent_30d', 0)} unsent 30d+")
        # Show top 3 most recent
        recent_stale = list_stale_candidates(limit=3)
        for s in recent_stale:
            preview = (s.get("content") or "").strip()[:120]
            lines.append(f"   • lesson #{s['lesson_id']} ({s['reason']}): {preview}")
    else:
        lines.append("🧹 *Memory hygiene*: all memory entries fresh")
    lines.append("")

    # Phase 5: branch housekeeping — keep disk usage bounded
    n_pruned = branch.prune_old_branches(keep=20)
    active_branches = branch.list_branches()
    n_active = len(active_branches)
    if n_active > 0:
        n_revertable = sum(1 for b in active_branches if b.get("applied_rows", 0) > 0)
        lines.append(f"🌿 *Branches*: {n_active} active, {n_revertable} with applied changes "
                     f"(revertable via `python src/apply.py --revert <cycle_id>`)")
        if n_pruned > 0:
            lines.append(f"   pruned {n_pruned} old branch(es) this run")
    else:
        lines.append("🌿 *Branches*: no active branches")
    lines.append("")

    lines.append("_Reply with `approve #N` / `reject #N` / `modify #N: <note>` to act on proposals._")
    return "\n".join(lines)


def run():
    """Entry point for the weekly cron."""
    chat_id = get_target_chat()
    if not chat_id:
        print("[digest] No Telegram chat id resolved; aborting")
        return
    msg = build_weekly_digest()
    print(f"[digest] Sending {len(msg)}-char digest to {chat_id}")
    _send_telegram(chat_id, msg)
    # Mark lessons as sent
    lessons = unsent_lessons(limit=100)
    if lessons:
        mark_lessons_sent([l["id"] for l in lessons])
    print(f"[digest] Marked {len(lessons)} lessons as sent")


if __name__ == "__main__":
    run()
