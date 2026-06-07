# Self-Improvement Loop — Roadmap to v2

Six additions, in dependency order. Phases 1-4 are the cheap "coverage
gap" fixes (each is a single file or schema migration). Phase 5 is the
benchmark harness — bigger but the highest-leverage piece. Phase 6
turns the harness into a promotion gate.

Estimated effort is honest, not optimistic. Schedule is what fits
inside the existing cron rhythm without breaking the loop.

## Phase 1 — Cost & latency regression signal  (~2 hours, ship)

**Closes gap #2 (latency/cost regression).**

**What:** measure wall time and LLM-token-equivalent of every loop
step. Store on each `proposals` and `judge_verdicts` row. Add a
`cycle_stats` table for per-cycle aggregates.

**Why now:** cheapest possible signal. You have a `urllib` call site
in propose and judge — one decorator wraps it. You have a
`propose.run()` and `judge.judge_batch()` — `time.monotonic()` deltas
between entry and exit. You already log to `logs/cycle_*.log`. No
new infra.

**Schema:**
```sql
ALTER TABLE proposals ADD COLUMN propose_ms INTEGER;
ALTER TABLE proposals ADD COLUMN propose_input_tokens INTEGER;
ALTER TABLE proposals ADD COLUMN propose_output_tokens INTEGER;
ALTER TABLE judge_verdicts ADD COLUMN judge_ms INTEGER;
ALTER TABLE judge_verdicts ADD COLUMN judge_input_tokens INTEGER;
ALTER TABLE judge_verdicts ADD COLUMN judge_output_tokens INTEGER;

CREATE TABLE cycle_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at REAL NOT NULL,
    finished_at REAL NOT NULL,
    duration_ms INTEGER NOT NULL,
    steps TEXT NOT NULL,           -- JSON: {propose: bool, judge: bool, apply: bool, grade: bool}
    proposals_mined INTEGER,
    proposals_generated INTEGER,
    judge_calls INTEGER,
    grade_graded INTEGER,
    grade_skipped INTEGER,
    error TEXT
);
CREATE INDEX idx_cycle_stats_started ON cycle_stats(started_at);
```

**Files:**
- `src/db.py` — schema migration in `init_db()` (idempotent: try/except on ALTER)
- `src/propose.py` — wrap `_call_m3`, time + count tokens from response.usage
- `src/judge.py` — same
- `src/loop.py` — record cycle_stats at end
- `src/digest.py` — add a `⏱ Cycle health: median Xs, p95 Ys, token cost $Z` line

**Token cost:** approximate from `usage.prompt_tokens` /
`usage.completion_tokens` × published model rate ($3/$15 per 1M for
m3, $0.30/$1.20 for m2.7 — read from env vars, fall back to zero).

**Signal it produces:** "median cycle is 8s, p95 is 45s, last week it
was 5s / 20s — something got slower." Catches perf drift weeks before
you'd notice manually.

**Pitfall:** urllib responses don't always include `usage` unless you
pass `stream=False` (default) and the API returns it. Handle the
absent case — store NULL, don't crash.

---

## Phase 2 — Memory staleness check  (~1 hour, ship)

**Closes gap #6 (memory rot).**

**What:** every cycle, scan `lessons_learned` for entries older than
30 days. For each, check whether the source session still exists in
`~/.hermes/sessions/`. If the source is gone OR the lesson hasn't
been surfaced (re-sent) in 30 days, write a "stale_candidate" row.

**Why now:** dead simple SELECT, no LLM call, no prompt engineering.
A new cron step or piggyback on `grade_outcomes.py`.

**Schema:**
```sql
CREATE TABLE stale_memory_candidates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    lesson_id INTEGER NOT NULL,
    detected_at REAL NOT NULL,
    reason TEXT,                  -- 'source_gone' | 'unsent_30d' | 'both'
    FOREIGN KEY (lesson_id) REFERENCES lessons_learned(id)
);
```

**Files:**
- `src/db.py` — table + helper `detect_stale_memory(days=30)`
- New `src/memory_hygiene.py` — runs `detect_stale_memory` + writes
  candidates
- `src/loop.py` — add as step 3.6, no LLM, runs after grade
- `src/digest.py` — add a `🧹 Memory hygiene: N stale candidates (K
  with source gone)` line. The candidates wait for thomas's manual
  decision — don't auto-delete.
- `src/propose.py` — inject the candidates into the proposer prompt
  the same way negative patterns are injected: "Existing memory
  entries flagged as stale: [list]. If you have new evidence these
  are still correct, ignore. Otherwise, propose a removal or refresh."

**Signal it produces:** "you have 14 memory entries that haven't been
surfaced in 30+ days. 4 of them reference sessions that no longer
exist. Confirm or delete."

**Pitfall:** don't auto-delete. Memory deletion is destructive and
memory entries might be load-bearing for current behavior. Always
queue for thomas.

---

## Phase 3 — Pipeline error feedback  (~2 hours, ship)

**Closes gap #7 (parse failures / API timeouts invisible to the loop).**

**What:** when the proposer or judge emits output that fails to
parse, when the API call times out, or when a diff fails to apply —
write a `lessons_learned` row with `category='gap'` and the failure
context. The next proposer cycle sees it in the "lessons this
window" injection (you already inject negative patterns; do the same
for pipeline gaps).

**Why now:** these errors are already being silently caught by
`try/except` blocks. Just write them to the DB instead of printing
to stderr.

**Schema:** no new table. Reuse `lessons_learned` with `category='gap'`.

**Files:**
- `src/propose.py` — on `_parse_proposals` failure or API timeout,
  call `db.add_lesson('gap', f'propose failure in session X: {msg}',
  source=sid)`
- `src/judge.py` — same for unparseable verdict
- `src/apply.py` — on diff-apply failure, `db.add_lesson('gap',
  f'apply failure on proposal #N: {reason}')`
- `src/propose.py` — the proposer already reads `lessons_learned`
  via the digest feedback chain, but doesn't see them in real time.
  Add direct query: `recent_lessons(days=7, limit=10)` to the system
  prompt construction, alongside the negative patterns injection.

**Signal it produces:** "Last cycle, the judge failed to parse
verdicts 3 times because m2.7 wrapped its output in
` ```json ` fences. Proposer should warn the user or add a
parsing-improvement proposal."

**Pitfall:** the gap categories can flood the proposer's context if
they pile up. Cap at 10 most recent in the 7-day window. Older ones
go to digest, not the prompt.

---

## Phase 4 — Self-referential cron failure feedback  (~1 hour, ship)

**Closes gap #8 (loop's own infrastructure errors invisible).**

**What:** when a cron job for the loop itself (sri-propose,
sri-judge, sri-apply, sri-digest) fails or times out, write a
synthesized "session" record to the DB and let the next proposer
cycle mine it as a session.

**Why now:** this is what just bit you with the 120s timeout. The
loop should be telling itself about it, not relying on thomas to
notice the cron failure alert.

**Schema:**
```sql
CREATE TABLE self_incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    detected_at REAL NOT NULL,
    job_name TEXT NOT NULL,
    incident_type TEXT NOT NULL,  -- 'timeout' | 'exit_nonzero' | 'no_output'
    detail TEXT,
    last_log_lines TEXT
);
```

**Files:**
- New `src/incident_watcher.py` — runs in a 4th cron job, every 6h.
  Inspects `logs/cycle_*.log`, `logs/propose_*.log`, `logs/judge_*.log`
  for the last 24h. Detects:
  - Logs older than 1h with a timestamp that hasn't been written to
    in 4h (job died silently)
  - Exit codes in the most recent log not equal to 0
  - Cycle wall-time > 110s (approaching cron 120s cap)
  On any detection, write to `self_incidents` and synthesize a
  `sessions_mined`-style marker.
- `src/propose.py` — the next time it picks sessions to mine, also
  include one synthetic "session" containing the incident detail
  ("sri-propose timed out at 2026-06-07 08:04 UTC, last log line: …
  This is the 2nd timeout in 3 days. Propose a fix."). Cap at 1
  synthetic session per cycle.

**Signal it produces:** "The sri-propose job has timed out twice
this week. Root cause: urllib timeout was 120s, same as cron cap.
Propose: lower urllib timeout to 30s + add API-call circuit breaker
to abort cycle on first timeout."

**Pitfall:** don't let incidents flood the proposer. Cap at 1
synthetic session per cycle, and the watcher's detail-extraction
should trim to the most recent actionable item.

---

## Phase 5 — Benchmark harness: design  (~1 day, the big one)

**Closes: rubric gaming, single-point-of-failure on override rate, and
the "things thomas approves" vs "things that work" gap that the
ChatGPT stub correctly identified.**

### Concept

A **test set** of 10-30 hand-picked historical hermes sessions,
labeled with what a "good response" looks like (manual thomas-label
or implicit "no correction happened" → good). A **scorer** that
replays a session through the agent with the *current* agent
config (baseline) and with the *proposed* config (candidate), and
asks m2.7 to judge which response is better, or rates each on a
0-1 scale.

The harness runs in **sandbox mode** (not on the live session DB).
It only ever reads snapshots. The candidate config is applied to a
copy of the agent's relevant files in `/tmp/sri-bench-XXXX/`.

### Components

1. **Test set** — `data/benchmark/sessions.jsonl`
   - Hand-curated. Start with 10 sessions where thomas explicitly
     corrected the agent (label = "bad response") and 10 where the
     agent's response was clean (label = "good response").
   - Each entry: `{session_id, transcript_path, label, expected_signal}`
   - `expected_signal` is free text: "should not use grep without rg
     fallback" or "should ask one-question-at-a-time when ambiguous"

2. **Scoring function** — `src/bench/score.py`
   - Take a session + an agent config (skills dir, memory dir)
   - Replay the session through the agent in a sandboxed subprocess
     with that config
   - Capture the agent's response
   - Ask m2.7 to rate response quality 0-1 against the label
   - Return `{score, reasoning, transcript_diff}`

3. **Experiment runner** — `src/bench/run.py`
   - For a proposed change (skill patch, memory add), build:
     - `baseline_config`: current skills + memory (snapshot)
     - `candidate_config`: baseline + the proposed change applied
   - Run scoring function on each across all 10-30 test sessions
   - Compute: baseline_score, candidate_score, delta, p_value (if
     cheap — t-test on paired scores)
   - Output: `{baseline: 0.72, candidate: 0.81, delta: +0.09,
     p: 0.03, n: 30, per_session: [...]}`

4. **Sandbox** — `src/bench/sandbox.py`
   - For each run, create `/tmp/sri-bench-<uuid>/`
   - Symlink (not copy) the read-only files (skills, memory,
     sessions). The candidate change is *only* applied to the
     candidate copy.
   - The subprocess reads from the sandbox. It cannot write back
     to the real ~/.hermes/ because apply.py's path validation
     already enforces this, but we also use a chdir-and-isolate
     approach to be safe.

5. **Promotion gate** — modified `src/apply.py`
   - Before applying a merged proposal, run the benchmark harness
   - If candidate_score <= baseline_score, OR delta < threshold
     (start at 0.05), OR p > 0.10, **refuse to apply** and write
     a `lessons_learned` row explaining why
   - Thomas gets a Telegram alert: "Proposal #N failed benchmark:
     candidate 0.71 < baseline 0.74. Holding for review."

### Cost & timeline

- **Test set curation:** 1-2 hours of thomas's time picking 20
  sessions. Hardest part — the loop can't do this for you.
- **Scoring function:** half a day. Replay infrastructure is
  fiddly. m2.7 as judge-of-judges is the cheap path; a real
  eval is months of work.
- **Experiment runner:** 2-3 hours. Mostly glue.
- **Sandbox:** 2-3 hours. Path validation is the tricky part.
- **Promotion gate integration:** 1-2 hours. Modify apply.py to
  consult the harness first.

**Total: ~1.5 days of engineering, plus thomas-curated test set.**

### What it buys

- The loop stops promoting changes that thomas reflexively approves
  but that *actually hurt* the agent (caught by benchmark, not by
  override rate)
- A second promotion signal independent of thomas's mood
- A/B test before promotion: every change gets measured, not
  inferred-after-the-fact

### What it doesn't buy

- It can't measure things you don't have a test for. Test set
  coverage is the ceiling.
- The judge-of-judges (m2.7 scoring m3's responses) inherits all
  of m2.7's biases. Real eval needs human labels.
- 10-30 sessions is small. Statistical power is weak. The first
  few months will produce noisy results.

---

## Phase 6 — Wire benchmark to the weekly digest  (~1 hour, ship)

**Closes: visibility gap. Now that we have benchmark results, show
them.**

**What:** the digest gets a new section showing the last 4 weeks of
benchmark scores (sparkline via Telegram-safe chars: ▁▂▃▄▅▆▇█). If
the current week's median is below last week's, mark with ⚠️.

**Files:**
- `src/digest.py` — query `cycle_stats` and benchmark results table
  (new), build sparkline
- New `src/bench/track.py` — write benchmark results to
  `benchmark_runs` table after each run

**Signal:** "This week the agent scored 0.74 (▼ 0.04). Last week's
patch (proposal #12) regressed. Review or revert."

---

## Total scope and order

| Phase | What | Effort | Closes gap |
|---|---|---|---|
| 1 | Cost & latency | 2h | #2 |
| 2 | Memory staleness | 1h | #6 |
| 3 | Pipeline error feedback | 2h | #7 |
| 4 | Self-referential cron | 1h | #8 |
| 5 | Benchmark harness | 1.5d | everything (#1, #2, rubric gaming) |
| 6 | Digest wiring | 1h | visibility |

**Phases 1-4 land as a single PR** (~6 hours, ship same week).
**Phase 5 lands as a separate, larger PR** (~1.5 days, ship next
week after thomas curates the test set).
**Phase 6 is part of the Phase 5 PR** (digest wiring is trivial once
bench is real).

**What I recommend:** start with 1-4 (cheap coverage). Run the loop
for a week under the new instrumentation. Use that data to inform
the test set for Phase 5 — you want benchmark sessions that
represent *real failure modes you've already seen*, not hypothetical
ones. Then build the harness against known targets.
