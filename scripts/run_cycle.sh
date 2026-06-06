#!/usr/bin/env bash
# Cron entrypoint: run one propose+judge cycle silently.
# Used by cronjob (no_agent=True) — empty stdout = silent (no Telegram).
# Exit non-zero on failure → cron sends error alert to thomas.

set -e
cd /home/thomas/self-recursive-improvement || exit 1

LOG=logs/cycle_$(date -u +%Y%m%d_%H%M%S).log

python3 src/loop.py --skip-apply --max 3 > "$LOG" 2>&1
EXIT=$?

# Tail the last 30 lines so cron delivers a brief summary
tail -30 "$LOG"

# Exit non-zero if loop failed, so cron can alert
exit $EXIT
