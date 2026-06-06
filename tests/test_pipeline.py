"""
Smoke tests for the deterministic plumbing. Run before relying on the loop.

  python3 tests/test_db.py
  python3 tests/test_miner.py
  python3 tests/test_pipeline.py
"""

import os
import sys
import shutil
import tempfile
import json
from pathlib import Path

# Use a temp DB so we don't pollute real state
TEST_DIR = Path(tempfile.mkdtemp(prefix="loop_test_"))
os.environ["LOOP_TEST_DIR"] = str(TEST_DIR)

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import db


def test_db_lifecycle():
    """Schema creation, insert, query, override detection."""
    # Override default DB path
    db.DB_PATH = TEST_DIR / "test.db"
    db.init_db()

    # Add a proposal
    pid = db.add_proposal(
        target_kind="skill_patch",
        diff="<<< old === new >>>",
        rationale="test",
        evidence="session xyz",
        target_path="/tmp/test_skill.md",
        source_session_id="test_session",
        confidence=0.8,
    )
    assert pid > 0, "proposal id not returned"

    # Judge approves
    db.add_judge_verdict(pid, "MiniMax-M2.7", "approve", 0.9, "looks good")

    # Thomas agrees
    db.add_thomas_feedback(pid, "approve", "agreed")
    p = db.pending_proposals()
    assert p == [], f"expected no pending, got {p}"
    print("  ✓ db: insert → judge → agree")

    # Now an override case
    pid2 = db.add_proposal(
        target_kind="memory_add",
        diff="Reply concisely",
        rationale="maybe shorter replies",
        evidence="none",
        target_path="/tmp/mem.md",
    )
    db.add_judge_verdict(pid2, "MiniMax-M2.7", "approve", 0.6, "ok")
    db.add_thomas_feedback(pid2, "reject", "no, this is too generic")
    stats = db.override_stats()
    assert stats["overrides"] == 1, f"expected 1 override, got {stats}"
    assert stats["override_rate"] == 0.5, f"expected 50% rate, got {stats}"
    print(f"  ✓ db: override detection ({stats})")

    # Lessons
    db.add_lesson("pattern", "User prefers caveman-concise", "test")
    lessons = db.unsent_lessons()
    assert len(lessons) == 1
    db.mark_lessons_sent([lessons[0]["id"]])
    assert db.unsent_lessons() == []
    print("  ✓ db: lessons sent tracking")

    # Rubric
    rubric = db.latest_rubric()
    assert rubric["version"] == 1
    db.save_rubric("new rubric text", parent_version=1, notes="test v2")
    rubric2 = db.latest_rubric()
    assert rubric2["version"] == 2
    print("  ✓ db: rubric versioning")


def test_miner_basic():
    """Miner reads session files correctly."""
    from miner import load_session, format_for_proposer, unmined_sessions

    # Reset DB to a fresh test DB before miner reads it
    db.DB_PATH = TEST_DIR / "test2.db"
    db.init_db()

    # Create a fake session file (timestamp must be in the past)
    sess_dir = Path.home() / ".hermes" / "sessions"
    test_file = sess_dir / "20200101_120000_testminersession.jsonl"
    test_file.parent.mkdir(parents=True, exist_ok=True)
    with open(test_file, "w") as f:
        f.write(json.dumps({
            "role": "user", "content": "Hello", "timestamp": "2020-01-01T12:00:00"
        }) + "\n")
        f.write(json.dumps({
            "role": "assistant", "content": "Hi there", "timestamp": "2020-01-01T12:00:05"
        }) + "\n")
    try:
        sid, count, msgs = load_session(test_file)
        assert sid == "20200101_120000_testminersession"
        assert count == 2
        assert msgs[0]["role"] == "user"
        print(f"  ✓ miner: load_session → {count} msgs")

        formatted = format_for_proposer(sid, count, msgs)
        assert "user" in formatted and "assistant" in formatted
        print(f"  ✓ miner: format_for_proposer → {len(formatted)} chars")

        # Bypass real session mining by checking the test session is parseable.
        # Full unmined_sessions test would require mocking the session dir.
        unmined = unmined_sessions(limit=200, min_age_hours=0)
        # Don't assert it contains our test file (real session ordering varies)
        # Just verify it returns a list of tuples
        assert isinstance(unmined, list)
        if unmined:
            assert len(unmined[0]) == 3  # (sid, path, count)
        print(f"  ✓ miner: unmined_sessions returns {len(unmined)} candidates")
    finally:
        test_file.unlink(missing_ok=True)


def test_judge_parser():
    """Judge verdict parser handles malformed output gracefully."""
    from judge import _parse_verdict

    # Clean JSON
    v = _parse_verdict('{"verdict": "approve", "score": 0.8, "reasoning": "ok"}')
    assert v["verdict"] == "approve" and v["score"] == 0.8
    print("  ✓ judge: clean JSON parses")

    # Wrapped in fences
    v = _parse_verdict('```json\n{"verdict": "reject", "score": 0.2}\n```')
    assert v["verdict"] == "reject"
    print("  ✓ judge: fenced JSON parses")

    # Garbage in → reject by default
    v = _parse_verdict("I think this is fine")
    assert v["verdict"] == "reject" and v["score"] == 0.0
    print("  ✓ judge: garbage → reject")


if __name__ == "__main__":
    print("test_db_lifecycle")
    test_db_lifecycle()
    print("test_miner_basic")
    test_miner_basic()
    print("test_judge_parser")
    test_judge_parser()
    print("\nAll tests passed.")
    # Cleanup
    shutil.rmtree(TEST_DIR, ignore_errors=True)
