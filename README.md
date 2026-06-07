# self-recursive-improvement

An autonomous, *self-improving* agent loop. Built so an AI agent (Hermes /
MiniMax-M3) can propose changes to its own skills, memory, and judging
rubric — get an independent second-opinion from a different model
(MiniMax-M2.7) — and queue them for human review with the model's
reasoning attached.

The human's final decision becomes the training signal. Override rate
is the metric that drives the rubric to refine itself.

```
┌─────────────────────────────────────────────────────────────┐
│                   SELF-IMPROVEMENT LOOP                     │
└─────────────────────────────────────────────────────────────┘

  ┌──────────┐    ┌──────────┐    ┌──────────┐    ┌──────────┐
  │ miner.py │ →  │ propose  │ →  │  judge   │ →  │  apply   │
  │ (cron)   │    │   .py    │    │   .py    │    │   .py    │
  │          │    │  M3 LLM  │    │ M2.7 LLM │    │ (writes  │
  │ Read     │    │ Generate │    │ Review   │    │  skills/ │
  │ sessions │    │ proposal │    │ vs       │    │  memory) │
  │          │    │ JSONL    │    │ rubric   │    │          │
  └──────────┘    └──────────┘    └──────────┘    └──────────┘
       ↑              ↑                                 │
       │              │                                 ↓
       │     ┌────────┴─────────┐             ┌──────────────────┐
       │     │ Patterns to AVOID │             │ grade_outcomes.py│
       │     │ (10 most recent  │             │ reverted/recor/  │
       │     │  thomas rejects) │             │ neutral 7d timer │
       │     └──────────────────┘             └──────────────────┘
       │                                              │
                                              ↑ thomas approves
                                              │ (Telegram reply)
   ╔══════════════════════════════════════════╧════════════╗
   ║           digest.py  (weekly Telegram summary)        ║
   ║  📊 judge health  ·  🧪 change outcomes  ·  📚 lessons ║
   ╠═══════════════════════════════════════════════════════╣
   ║     self_improve.py  (rubric auto-refine, monthly)    ║
   ╚═══════════════════════════════════════════════════════╝
```

## Why this exists

Single-model self-improvement is an echo chamber. The model proposes,
the model approves, biases get reinforced.

Splitting the loop across two models with **different gradients** plus
**explicit human override** kills the echo chamber. The m2.7 judge
catches obvious bad proposals. Thomas's overrides catch what neither
model sees. The override rate then drives the rubric to predict
Thomas's decisions better over time.

## Three-layer architecture

| Layer        | What it does                                 | Cadence    |
|--------------|----------------------------------------------|------------|
| **Proposer** | m3 reads sessions, generates skill/memory patches | every 8h |
| **Judge**    | m2.7 reviews each proposal against the rubric | every 8h  |
| **Human**    | Thomas approves / rejects via Telegram       | weekly    |
| **Grade**    | Grade prior applied changes as helped/reverted/recorrected/neutral | every 8h |
| **Digest**   | Weekly Telegram summary of lessons + pending | weekly    |
| **Self-improve** | Detects high override rate, proposes rubric update | monthly |

## Two feedback signals

The loop learns from two distinct signals. They measure different
things and catch different failure modes — read both weekly.

**1. Override rate** — judge calibration.
Did m2.7 predict Thomas's decision? Computed from
`thomas_feedback.was_overrides_judge`. A judge that's always
sycophantic looks perfect (0%) and is useless. A judge that's
correctly calibrated climbs toward 0% as the rubric improves.
*Metric the rubric auto-refine loop optimizes.*

**2. Help ratio** — change quality.
Did the *applied* change actually help? Computed by
`grade_outcomes.py` after each apply. Heuristics:

- **reverted** — current file content matches a backup newer than the apply
- **recorrected** — a later applied change re-touches the same region, OR
  a `correction`/`gap` lesson re-surfaces referencing the same path
- **neutral** — 7+ days pass with no revert/recorrection signal
- **helped** — *not auto-detected*; a proposal is implicitly helped
  when it survives and isn't followed by a correction

`help ratio = helped / (helped + reverted + recorected)`.
Catches the failure mode where override rate is fine but the loop
keeps producing low-quality changes that thomas approves reflexively.
Visible in the digest as the 🧪 line.

**3. Cost & latency (Phase 1).** Every cycle's wall time + token usage
is recorded in `cycle_stats`. The digest shows median + p95 cycle
duration and an estimated $cost. Catches perf drift weeks before
thomas would notice.

**4. Memory hygiene (Phase 2).** Every cycle, `memory_hygiene.py`
flags lessons in `lessons_learned` that are either (a) older than 30
days and have never been sent in a digest, or (b) reference a source
session that's been deleted. Surfaced in the digest; never auto-deleted.

**5. Pipeline gaps (Phase 3).** API timeouts, JSONL parse failures,
and apply errors are written as `lessons_learned` with `category='gap'`.
The next proposer cycle sees them in its system prompt and can
propose a fix.

**6. Self-incidents (Phase 4).** `incident_watcher.py` detects when
the loop's own cron jobs (sri-propose, sri-judge, sri-apply) go
silent, fail, or run too long. Each incident is synthesized into a
session the proposer can mine on the next cycle.

**7. Per-cycle branch isolation (Phase 5).** Every `apply.py` cycle
snapshots the target files to `data/branches/<cycle_id>/` *before*
writing. If a change turns out to be bad, `python3 src/apply.py
--revert <cycle_id>` restores the files and marks all linked
`applied_outcomes` rows as `reverted` with `cycle_id` and evidence.
This is the loop's blast-radius boundary. Targets (`~/.hermes/skills/`,
`~/.hermes/memories/`) are not git repos, so the branches live as
filesystem snapshots — bounded by `branch.prune_old_branches(keep=20)`
called from the weekly digest.

## How a proposal moves through the loop

1. **mine** — `miner.py` scans `~/.hermes/sessions/*.jsonl`, skips
   sessions already mined or too recent.

2. **propose** — m3 reads the session, follows the strict prompt in
   `prompts/proposer.md`, and emits JSONL. Empty response is
   `{"no_proposals": true, "reason": "..."}`. The system prompt
   has the active rubric + the 10 most recent "Patterns to AVOID"
   (thomas's recent rejections) injected at construction time.

3. **persist** — `db.py` stores the proposal in SQLite, with the
   current rubric version stamped on it.

4. **judge** — `judge.py` sends the proposal to m2.7 with the active
   rubric prompt. m2.7 emits strict JSON: `{verdict, score, reasoning}`.

5. **await thomas** — proposal sits in `pending` state. Weekly digest
   shows top proposals with judge reasoning attached.

6. **decide** — thomas replies `approve #N`, `reject #N`, or
   `modify #N: <note>`. Status moves to `merged` (for skills) or
   `rejected`/`overridden`. A `negative_patterns` row is captured
   for every reject/override — it's the input to the proposer's
   "Patterns to AVOID" injection on the next cycle.

7. **apply** — `apply.py` (cron, every 8h) applies merged proposals
   to the actual skill/memory files, with backups to
   `data/backups/`. Pinned/hub-installed skills are skipped. Every
   successful apply writes a row to `applied_outcomes` for later
   grading.

8. **grade** — `grade_outcomes.py` (runs every cycle, step 3.5)
   grades prior applied changes as `helped` / `neutral` / `reverted`
   / `recorrected` based on file-backup / re-touch / lesson-resurface
   heuristics. Rows stay `unknown` for 7 days before defaulting to
   `neutral`.

9. **learn** — `self_improve.py` (monthly) computes override rate.
   If > 30%, m3 is asked to propose a refined rubric. The refinement
   itself goes through the same propose→judge→thomas loop.

10. **digest** — `digest.py` (weekly, Mondays 09:00) builds a single
    Telegram message and sends it to Thomas's home channel. It
    reports on every signal above (judge health, cycle health,
    change outcomes, lessons, pending proposals, pipeline errors,
    self-incidents, memory hygiene, branch count) and marks all
    lessons as `sent_in_digest_at=now()` so the next run is quiet.

## What the weekly digest contains

Sent to `TELEGRAM_HOME_CHANNEL` every Monday 09:00 by the
`sri-weekly-digest` cron job. Sections in order:

| Section | What it tells you |
|---------|-------------------|
| 📊 Judge health | Total proposals judged + override rate. The loop's only calibration signal. |
| 📜 Active rubric | Current `rubric_versions.version` (bumps after rubric self-eval). |
| ⏱ Cycle health | Wall time median/p95/min/max for the last 10 cycles + token cost. Catches perf drift weeks early. |
| 🧪 Change outcomes | Graded applied changes (helped / neutral / reverted / recor) + help ratio. Empty for the first 7 days. |
| 📚 Lessons this week | Unsorted list of `lessons_learned` rows (patterns, gaps, lessons, corrections). Top 15 + a "more" line. |
| ⏳ Pending your review | Proposals still waiting for Thomas's decision, with judge verdict + rationale. |
| ⚠️ Pipeline errors (7d) | Count of propose / judge / apply failures in the last week. |
| 🪞 Self-incidents (7d) | Counts of incidents the loop detected in its own cron jobs (silent jobs, wall-time overruns, etc.) + most recent. |
| 🧹 Memory hygiene | Stale `lessons_learned` candidates (source gone, unsent 30d+, etc.) — never auto-deleted. |
| 🌿 Branches | Active cycle snapshots + how many were pruned this run. |
| Reply hint | `_Reply with approve #N / reject #N / modify #N: <note>..._` |

The message is sent via the Telegram Bot API in 3900-char chunks
with `MarkdownV2` and a plain-text fallback on parse error. Lessons
are marked `sent_in_digest_at` after a successful send so the same
content isn't re-shipped.

## What "approved" actually does

| target_kind     | On approve                          |
|-----------------|-------------------------------------|
| `skill_patch`   | apply.py modifies the SKILL.md      |
| `skill_create`  | apply.py writes a new SKILL.md      |
| `memory_add`    | appends entry to memories/          |
| `rubric_update` | bumps `rubric_versions.version`     |

All writes are backed up to `data/backups/<timestamp>_<file>`. Pinned
skills (under `~/.hermes/skill-assets/`) are never auto-modified.

## Safety rails

- **Pinned skills** — protected from auto-edit. PR upstream instead.
- **Backup before write** — every apply.py write goes to a timestamped backup.
- **Path validation** — refuses paths outside `$HOME` or `/tmp`.
- **Idempotency** — re-running apply.py is a no-op (DONE markers).
- **Schema migrations** — `db.init_db()` uses `CREATE IF NOT EXISTS`.
- **Parser robustness** — judge/proposer outputs are tolerated when wrapped
  in fences, partial, or contain <think> blocks (m3 emits those).

## Files

```
self-recursive-improvement/
├── README.md                  # this file
├── docs/
│   └── roadmap-to-v2.md       # Phase 5+ spec
├── src/
│   ├── db.py                  # SQLite state store + schema
│   ├── miner.py               # session reader
│   ├── propose.py             # m3 proposer runner (injects avoid-list)
│   ├── judge.py               # m2.7 reviewer
│   ├── apply.py               # writes to skills/memory (logs outcomes)
│   │                          #   --branches  list cycle snapshots
│   │                          #   --revert <id>  restore a cycle's files
│   │                          #   --prune-branches KEEP  drop old branches
│   ├── branch.py              # Phase 5: per-cycle snapshot/revert
│   ├── grade_outcomes.py      # heuristic outcome grader
│   ├── memory_hygiene.py      # Phase 2: stale lesson detection
│   ├── incident_watcher.py    # Phase 4: self-referential cron health
│   ├── digest.py              # weekly Telegram digest
│   ├── self_improve.py        # rubric auto-refine
│   ├── loop.py                # orchestrator (one cycle)
│   ├── checkpoint.py          # judge calibration state (load/save)
│   └── rubric.py              # versioned judge rubric
├── prompts/
│   └── proposer.md            # m3 system prompt
├── tests/
│   ├── test_pipeline.py       # smoke tests
│   └── test_branch_full.py    # Phase 5 + apply + digest integration (47 checks)
├── data/                      # loop.db, branches/, backups/
└── logs/
```

## Quick start

```bash
# Smoke test the deterministic plumbing
python3 tests/test_pipeline.py

# Full Phase 5 + apply + digest integration (47 checks)
python3 tests/test_branch_full.py

# Run one full cycle (propose + judge, no apply)
python3 src/loop.py --skip-apply --max 3

# Dry run — see what would be proposed without writing
python3 src/loop.py --dry-run --max 3

# Just re-judge pending proposals (e.g. after rubric update)
python3 src/loop.py --judge-only

# Grade prior applied outcomes (manual run; cron does this every cycle)
python3 src/grade_outcomes.py --dry-run

# Branch management (Phase 5)
python3 src/apply.py --branches          # list all cycle snapshots
python3 src/apply.py --revert <cycle_id> # restore files + mark reverted
python3 src/apply.py --prune-branches 10 # drop oldest 10 (safe ones only)

# Force rubric self-eval
python3 src/loop.py --self-improve --dry-run

# Manually trigger the weekly digest (sends to Telegram)
cd ~/self-recursive-improvement && source ~/.hermes/.env && python3 src/digest.py
```

## Environment

- `MINIMAX_API_KEY` — required for m3/m2.7 calls
- `TELEGRAM_BOT_TOKEN` — required for weekly digest delivery
- `TELEGRAM_HOME_CHANNEL` — Thomas's Telegram chat id. The digest
  resolver checks this env var first, then falls back to
  `~/.hermes/config.yaml` (`home_channels.telegram`).

For cron jobs running in the user shell, `source ~/.hermes/.env` loads
both tokens. The `sri-weekly-digest` cron job does this automatically
via its prompt.

## Cron schedule (active)

The loop runs as four jobs in `~/.hermes/cron/jobs.json`:

| Job | Schedule | Mode | Purpose |
|-----|----------|------|---------|
| `sri-propose`        | `0 */8 * * *`   | no_agent script | Mine sessions + generate proposals |
| `sri-judge`          | `5 */8 * * *`   | no_agent script | M2.7 reviews pending proposals |
| `sri-apply-merged`   | `0 4 * * *`     | agent           | Merge approved proposals to skills/memory |
| `sri-weekly-digest`  | `0 9 * * 1`     | agent           | Send weekly Telegram digest (see below) |
| `sri-rubric-self-eval` | `0 10 1 * *` | agent           | Monthly rubric refinement |
| `sri-phase5-kickoff` | `once 2026-06-14 10:00` | agent (skill: plan) | Build the benchmark harness |

Manual cron entries (if running outside Hermes):
```cron
0 */8 * * *   cd ~/self-recursive-improvement && python3 src/loop.py --skip-apply >> logs/cron.log 2>&1
0 4 * * *    cd ~/self-recursive-improvement && python3 src/apply.py >> logs/cron.log 2>&1
0 9 * * 1    cd ~/self-recursive-improvement && source ~/.hermes/.env && python3 src/digest.py >> logs/cron.log 2>&1
0 10 1 * *   cd ~/self-recursive-improvement && python3 src/self_improve.py >> logs/cron.log 2>&1
```

## Design choices and trade-offs

**Why JSONL from the proposer?** Any free-form prose gets parsed wrong
at some point. JSONL is line-oriented, easy to recover partial output,
and forces the model to commit to a structured shape.

**Why a separate database instead of editing `~/.hermes/skills/` directly?**
The DB is the audit trail. You can ask "what proposals did m3 make in
the last 30 days?" and get an answer. The skills directory has no
history. A git-tracked DB is the loop's "model registry."

**Why override rate as the metric?** It's the only objective signal
that the loop has. A judge that always says "approve" has 0% override
rate and looks perfect — but it's also useless. The override rate
combined with the override *reasons* (Thomas's notes) tells you whether
the judge is calibrated or just agreeing.

**Why not auto-apply based on judge verdict alone?** Because a 0%
override rate is the goal, not a 100% auto-apply rate. If the loop
never asks for human input, the human signal disappears and the rubric
stops improving. The loop is *designed* to keep Thomas in the loop
until the override rate drops below 10% — at which point the digest
will tell you, and you can choose to widen auto-approval criteria.

## Security

This loop is plumbing for an AI agent's self-modification. By design it
writes to skill files and memory. Treat its permissions accordingly.

**Required environment variables (never commit):**
- `MINIMAX_API_KEY` — used by `propose.py`, `judge.py`, `self_improve.py`
- `TELEGRAM_BOT_TOKEN` — used by `digest.py`

Both are read via `os.environ.get(...)` and never persisted. The
`.gitignore` excludes `.env`, `*.log`, and the runtime database.

**Audit the data directory before sharing.** `data/loop.db` accumulates
proposals, judge verdicts, and your override notes. It's gitignored,
but if you copy the repo elsewhere (USB, zip), scrub it first.

**Threat model:**
- A pinned-skill change requires explicit approval and a backup.
- A skill file outside `~/.hermes/skills/` is rejected.
- A path outside `$HOME` or `/tmp` is rejected.
- A `.env` or credential file is never read by the loop.

**What this is NOT:** it does not sandbox the LLM. The m3 proposer can
emit any diff it wants; the rubric and judge catch most bad ones, but
your override is the last line of defense. Read the weekly digest.

**Forking:** the loop's calibration is specific to one user (thomas).
If you fork it, start with override_rate=undefined, expect to spend
the first month tuning the rubric, and treat the proposer's early
output as low-trust.

- **Session content is large.** 200+ message sessions get head+tail
  truncated, missing middle. Recurring patterns in the middle can be
  missed. A future improvement: pass the full session, ask the model
  to summarize first, then propose.
- **No diff validation.** apply.py tries to apply diffs and rolls back
  on failure, but it doesn't pre-validate the syntax. A bad proposal
  with a broken diff blocks itself in `apply.py` silently. Consider
  adding a "dry-run apply" step in the propose phase.
- **No concurrency control.** Two cron jobs running propose at the
  same time can both mine the same session. Currently the
  `sessions_mined` table prevents double-recording proposals, but
  the LLM cost is duplicated. The schedule (every 8h) makes this
  unlikely in practice.
- **Outcome grader is heuristic, not measured.** `grade_outcomes.py`
  detects `reverted` (backup-matches-current) and `recorrected`
  (later change re-touches us, or a correction lesson re-surfaces),
  then defaults to `neutral` after 7 days. It never *positively*
  detects "helped" — a change is implicitly helped by surviving
  without a correction. False-positive reverts are possible if a
  change naturally produces content identical to its backup (rare).
  Use the help ratio as a *trend*, not a precise count.

## License

MIT
