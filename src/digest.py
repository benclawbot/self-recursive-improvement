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
)


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
    """Resolve Thomas's home chat id from hermes config."""
    cfg = HERMES_HOME / "config.yaml"
    if not cfg.exists():
        return ""
    try:
        import yaml
        with open(cfg) as f:
            data = yaml.safe_load(f) or {}
        for ch in data.get("home_channels", []) or []:
            if ch.startswith("telegram:"):
                return ch.split(":", 1)[1]
        # Try platforms
        plats = data.get("platforms", {})
        tg = plats.get("telegram", {})
        return tg.get("home_chat_id", "")
    except Exception:
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
