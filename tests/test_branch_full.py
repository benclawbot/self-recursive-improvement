"""
Comprehensive test of src/branch.py + apply.py + db.py + digest.py integration.

Test state is isolated to /tmp/sri_test_* so we never touch the real
~.hermes/skills/ files (except for the explicit end-to-end test 9, which
uses a throwaway test skill we create and clean up).

Run with: PYTHONPATH=src python3 tests/test_branch_full.py
Exit code 0 = all pass, non-zero = failures.
"""

import os
import sys
import json
import time
import shutil
import sqlite3
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import db
import branch


PASS = "✓"
FAIL = "✗"
results = []


def check(name: str, condition: bool, detail: str = ""):
    sym = PASS if condition else FAIL
    line = f"  {sym} {name}" + (f"  ({detail})" if detail else "")
    print(line)
    results.append((condition, name, detail))


# Make a real DB init — these tests will pollute loop.db
db.init_db()


# ──────────────────────────────────────────────────────────────────────
# Test 1: --branches CLI works
# ──────────────────────────────────────────────────────────────────────
print("\n[1] --branches CLI")
# Use one cycle to ensure something shows up
tmp = Path(tempfile.mkdtemp(prefix="sri_t1_"))
t1 = tmp / "x.md"; t1.write_text("hello")
cid = branch.cycle_start([t1])
# Now call the function directly (CLI argparse path tested separately)
branches = branch.list_branches()
check("list_branches returns >= 1", len(branches) >= 1, f"got {len(branches)}")
check("newest branch has our cycle_id", branches[0]["cycle_id"] == cid)
check("files_snapshotted == 1", branches[0]["files_snapshotted"] == 1, f"got {branches[0]['files_snapshotted']}")
shutil.rmtree(tmp)


# ──────────────────────────────────────────────────────────────────────
# Test 2: cycle_start creates snapshot for existing file
# ──────────────────────────────────────────────────────────────────────
print("\n[2] cycle_start snapshots existing files")
tmp = Path(tempfile.mkdtemp(prefix="sri_t2_"))
t2 = tmp / "a.md"; t2.write_text("ORIGINAL_A")
t2b = tmp / "b.md"; t2b.write_text("ORIGINAL_B")
cid = branch.cycle_start([t2, t2b])
branch_dir = Path("data/branches") / cid
check("branch dir exists", branch_dir.exists())
check("_meta.json exists", (branch_dir / "_meta.json").exists())
check("snapshot of a.md exists", (branch_dir / t2).exists())
check("snapshot of a.md matches original", (branch_dir / t2).read_text() == "ORIGINAL_A")
check("snapshot of b.md exists", (branch_dir / t2b).exists())
shutil.rmtree(tmp)


# ──────────────────────────────────────────────────────────────────────
# Test 3: cycle_start tracks created-in-cycle files
# ──────────────────────────────────────────────────────────────────────
print("\n[3] cycle_start tracks created-in-cycle files")
tmp = Path(tempfile.mkdtemp(prefix="sri_t3_"))
new_file = tmp / "new.md"  # does NOT exist
cid = branch.cycle_start([new_file])
branch_dir = Path("data/branches") / cid
meta = json.loads((branch_dir / "_meta.json").read_text())
check("new file recorded in created_in_cycle", str(new_file) in meta["created_in_cycle"])
check("new file NOT in snapshotted", str(new_file) not in meta["snapshotted"])
# The was_missing marker is at mirror.parent — branch_dir + mirrored path's parent
mirror = branch_dir / str(new_file).lstrip("/")
mirror_parent = mirror.parent
check("was_missing marker parent dir exists", mirror_parent.exists(),
      f"checking: {mirror_parent}")
check("was_missing marker file exists",
      any(f.name.startswith(".was_missing_") for f in mirror_parent.iterdir())
      if mirror_parent.exists() else False)
shutil.rmtree(tmp)


# ──────────────────────────────────────────────────────────────────────
# Test 4: revert restores file content exactly
# ──────────────────────────────────────────────────────────────────────
print("\n[4] revert restores file content exactly")
tmp = Path(tempfile.mkdtemp(prefix="sri_t4_"))
t4 = tmp / "restore.md"; t4.write_text("BEFORE\nsecond line\n")
cid = branch.cycle_start([t4])
t4.write_text("AFTER\nsecond line\n")
check("file was mutated", "AFTER" in t4.read_text())
r = branch.revert(cid)
check("revert returned ok", r["ok"])
check("file restored to original", t4.read_text() == "BEFORE\nsecond line\n")
shutil.rmtree(tmp)


# ──────────────────────────────────────────────────────────────────────
# Test 5: revert deletes files created in cycle
# ──────────────────────────────────────────────────────────────────────
print("\n[5] revert deletes files created in cycle")
tmp = Path(tempfile.mkdtemp(prefix="sri_t5_"))
new = tmp / "fresh.md"
cid = branch.cycle_start([new])  # snapshot of non-existent file
new.write_text("just created")
check("new file exists after write", new.exists())
r = branch.revert(cid)
check("revert returned ok", r["ok"])
check("new file deleted by revert", not new.exists())
check("created_cleaned lists the path", str(new) in r["created_cleaned"])
shutil.rmtree(tmp)


# ──────────────────────────────────────────────────────────────────────
# Test 6: revert marks applied_outcomes as 'reverted' with evidence
# ──────────────────────────────────────────────────────────────────────
print("\n[6] revert marks applied_outcomes as 'reverted'")
tmp = Path(tempfile.mkdtemp(prefix="sri_t6_"))
t6 = tmp / "x.md"; t6.write_text("orig")
cid = branch.cycle_start([t6])

# Add a real proposal + applied_outcome row linked to this cycle
pid = db.add_proposal(
    target_kind="skill_patch",
    target_path=str(t6),
    diff="<<<\norig\n===\nchanged\n>>>",
    rationale="t6 test",
    evidence="t6",
    confidence=1.0,
)
db.add_judge_verdict(pid, "test-judge", "approve", 1.0, "ok")
with db.conn() as c:
    c.execute("UPDATE proposals SET status = 'merged' WHERE id = ?", (pid,))
oid = db.record_applied_outcome(proposal_id=pid, target_kind="skill_patch",
                               target_path=str(t6), diff="orig→changed")
branch.attach_to_outcome(oid, cid)

# Sanity: outcome starts unknown
with db.conn() as c:
    row = c.execute("SELECT outcome, cycle_id FROM applied_outcomes WHERE id = ?", (oid,)).fetchone()
check("outcome starts as unknown", row["outcome"] == "unknown", f"got {row['outcome']}")
check("cycle_id is set", row["cycle_id"] == cid)

# Revert
r = branch.revert(cid)
check("outcomes_marked_reverted == 1", r["outcomes_marked_reverted"] == 1)

# Verify
with db.conn() as c:
    row = c.execute("SELECT outcome, outcome_evidence FROM applied_outcomes WHERE id = ?", (oid,)).fetchone()
check("outcome is now reverted", row["outcome"] == "reverted", f"got {row['outcome']}")
check("evidence mentions branch.py", "branch.py" in row["outcome_evidence"], f"got {row['outcome_evidence']}")
shutil.rmtree(tmp)


# ──────────────────────────────────────────────────────────────────────
# Test 7: revert is idempotent (second call no-op)
# ──────────────────────────────────────────────────────────────────────
print("\n[7] revert is idempotent")
tmp = Path(tempfile.mkdtemp(prefix="sri_t7_"))
t7 = tmp / "x.md"; t7.write_text("orig")
cid = branch.cycle_start([t7])
t7.write_text("changed")
r1 = branch.revert(cid)
content_after_first = t7.read_text()
check("first revert ok", r1["ok"])
check("content restored after first revert", content_after_first == "orig")
r2 = branch.revert(cid)
content_after_second = t7.read_text()
check("second revert also ok (idempotent)", r2["ok"])
check("content still 'orig' after second revert (idempotent)",
      content_after_second == "orig",
      f"got: {content_after_second!r}")
check("content matches after both reverts", content_after_first == content_after_second)
shutil.rmtree(tmp)


# ──────────────────────────────────────────────────────────────────────
# Test 8: prune keeps N most recent, skips ungraded
# ──────────────────────────────────────────────────────────────────────
print("\n[8] prune keeps N most recent, skips ungraded")
# Make 5 fresh branches
tmp = Path(tempfile.mkdtemp(prefix="sri_t8_"))
ids = []
for i in range(5):
    f = tmp / f"f{i}.md"; f.write_text(f"file {i}")
    ids.append(branch.cycle_start([f]))
    time.sleep(1.05)  # ensure unique timestamp seconds
check("5 branches created", len(branch.list_branches()) >= 5)
# Add an ungraded applied_outcome to the oldest
oldest = ids[0]
# Need a real proposal first (FK constraint)
pid_oldest = db.add_proposal(
    target_kind="skill_patch",
    target_path=str(tmp/"f0.md"),
    diff="<<<\nfile 0\n===\nchanged 0\n>>>",
    rationale="t8 oldest",
    evidence="t8",
    confidence=1.0,
)
with db.conn() as c:
    c.execute("UPDATE proposals SET status = 'merged' WHERE id = ?", (pid_oldest,))
oid = db.record_applied_outcome(proposal_id=pid_oldest, target_kind="skill_patch",
                                target_path=str(tmp/"f0.md"), diff="file 0→changed 0")
branch.attach_to_outcome(oid, oldest)
# Note: the recorded outcome is 'unknown' — prune should skip this branch
n_pruned = branch.prune_old_branches(keep=2)
check("prune returned number", isinstance(n_pruned, int))
remaining = [b["cycle_id"] for b in branch.list_branches()]
# Oldest should still be there because it has ungraded outcome
check("branch with ungraded outcome NOT pruned", oldest in remaining,
      f"oldest in remaining: {oldest in remaining}")
shutil.rmtree(tmp)


# ──────────────────────────────────────────────────────────────────────
# Test 9: end-to-end via apply.run() with a real merged proposal
# ──────────────────────────────────────────────────────────────────────
print("\n[9] end-to-end via apply.run() with real merged proposal")
# Use a throwaway skill in ~/.hermes/skills/_sri_test_/ so we can mutate
# and revert without touching real skills. (apply.py will reject if
# not under HERMES_SKILLS.)
real_test_skill_dir = Path("/home/thomas/.hermes/skills/_sri_test_skill_")
real_test_skill_dir.mkdir(parents=True, exist_ok=True)
skill_md = real_test_skill_dir / "SKILL.md"
skill_md.write_text("---\nname: sri-test-skill\n---\n# sri-test\nORIGINAL_LINE\n")
test_path = str(skill_md)

pid = db.add_proposal(
    target_kind="skill_patch",
    target_path=test_path,
    diff="<<<\nORIGINAL_LINE\n===\nCHANGED_LINE\n>>>",
    rationale="end-to-end test",
    evidence="t9",
    confidence=1.0,
)
db.add_judge_verdict(pid, "test-judge", "approve", 1.0, "ok")
with db.conn() as c:
    c.execute("UPDATE proposals SET status = 'merged' WHERE id = ?", (pid,))

import apply
n = apply.run()
check("apply.run() returned > 0", n > 0, f"got {n}")
check("file was modified", "CHANGED_LINE" in skill_md.read_text())

# Find the cycle_id for this apply
with db.conn() as c:
    row = c.execute(
        "SELECT cycle_id FROM applied_outcomes WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
        (pid,),
    ).fetchone()
cid = row["cycle_id"]
check("applied_outcomes.cycle_id is set", cid is not None, f"got {cid}")

# Revert via apply CLI command
import subprocess
r = subprocess.run(
    ["python3", "src/apply.py", "--revert", cid],
    cwd="/home/thomas/self-recursive-improvement",
    capture_output=True, text=True,
)
check("apply.py --revert exit 0", r.returncode == 0, f"stderr: {r.stderr[:200]}")
check("file restored to ORIGINAL_LINE", "ORIGINAL_LINE" in skill_md.read_text())
check("CHANGED_LINE gone", "CHANGED_LINE" not in skill_md.read_text())

# Verify outcome marked reverted
with db.conn() as c:
    row = c.execute(
        "SELECT outcome FROM applied_outcomes WHERE proposal_id = ? ORDER BY id DESC LIMIT 1",
        (pid,),
    ).fetchone()
check("outcome marked reverted", row["outcome"] == "reverted", f"got {row['outcome']}")

# Cleanup: remove the test skill + the cycle
shutil.rmtree(real_test_skill_dir)


# ──────────────────────────────────────────────────────────────────────
# Test 10: multiple files in one cycle all revert together
# ──────────────────────────────────────────────────────────────────────
print("\n[10] multiple files in one cycle all revert together")
tmp = Path(tempfile.mkdtemp(prefix="sri_t10_"))
files = []
for i in range(3):
    f = tmp / f"file{i}.md"; f.write_text(f"orig{i}"); files.append(f)
cid = branch.cycle_start(files)
# Mutate all 3
for i, f in enumerate(files):
    f.write_text(f"changed{i}")
# All should be CHANGED
mutated = all("changed" in f.read_text() for f in files)
check("all 3 files were mutated", mutated)
# Revert
r = branch.revert(cid)
check("3 files restored", len(r["restored"]) == 3, f"got {len(r['restored'])}")
restored = all("orig" in f.read_text() for f in files)
check("all 3 files restored to orig", restored)
shutil.rmtree(tmp)


# ──────────────────────────────────────────────────────────────────────
# Test 11: list_branches reports correct applied/reverted counts
# ──────────────────────────────────────────────────────────────────────
print("\n[11] list_branches reports correct counts")
tmp = Path(tempfile.mkdtemp(prefix="sri_t11_"))
t11a = tmp / "a.md"; t11a.write_text("a")
t11b = tmp / "b.md"; t11b.write_text("b")
cid = branch.cycle_start([t11a, t11b])

# Add 2 applied_outcomes linked to this cycle (real proposals for FK)
pid_a = db.add_proposal(target_kind="skill_patch", target_path=str(t11a),
                        diff="<<<a>>>", rationale="t11a", evidence="t11", confidence=1.0)
pid_b = db.add_proposal(target_kind="skill_patch", target_path=str(t11b),
                        diff="<<<b>>>", rationale="t11b", evidence="t11", confidence=1.0)
oid1 = db.record_applied_outcome(proposal_id=pid_a, target_kind="skill_patch",
                                 target_path=str(t11a), diff="x")
oid2 = db.record_applied_outcome(proposal_id=pid_b, target_kind="skill_patch",
                                 target_path=str(t11b), diff="y")
branch.attach_to_outcome(oid1, cid)
branch.attach_to_outcome(oid2, cid)

# Revert one
with db.conn() as c:
    c.execute("UPDATE applied_outcomes SET outcome='reverted' WHERE id = ?", (oid1,))

branches = {b["cycle_id"]: b for b in branch.list_branches()}
b = branches.get(cid)
check("branch in list_branches", b is not None)
if b:
    check("applied_rows == 2", b["applied_rows"] == 2, f"got {b['applied_rows']}")
    check("reverted_rows == 1", b["reverted_rows"] == 1, f"got {b['reverted_rows']}")
shutil.rmtree(tmp)


# ──────────────────────────────────────────────────────────────────────
# Test 12: digest builds without errors with branches in DB
# ──────────────────────────────────────────────────────────────────────
print("\n[12] digest builds without errors")
# Just import and call build_weekly_digest — it shouldn't crash
os.environ.pop("TELEGRAM_BOT_TOKEN", None)
import importlib
import digest
importlib.reload(digest)
try:
    msg = digest.build_weekly_digest()
    check("digest.build_weekly_digest returned", isinstance(msg, str))
    check("digest contains branch line", "Branch" in msg or "branch" in msg.lower(),
          f"snippet: {msg[msg.find('Branch')-20:msg.find('Branch')+60] if 'Branch' in msg else 'no match'}")
except Exception as e:
    check("digest.build_weekly_digest no exception", False, f"exception: {e}")


# ──────────────────────────────────────────────────────────────────────
# Final report
# ──────────────────────────────────────────────────────────────────────
total = len(results)
passed = sum(1 for ok, _, _ in results if ok)
print()
print("=" * 60)
print(f"  {passed}/{total} checks passed")
print("=" * 60)
if passed != total:
    print("  FAILED CHECKS:")
    for ok, name, detail in results:
        if not ok:
            print(f"    {FAIL} {name}  {detail}")
    sys.exit(1)
print("  ALL GREEN.")
sys.exit(0)
