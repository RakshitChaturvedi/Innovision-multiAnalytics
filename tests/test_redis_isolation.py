"""Tests never touch the platform's Redis: dedicated database, guarded."""
import os
import re
import subprocess
import sys
from pathlib import Path

from tests import redis_target

ROOT = Path(__file__).resolve().parents[1]


def test_default_test_database_is_15_and_not_0():
    assert int(os.environ.get("TEST_REDIS_DB", "15")) == redis_target.TEST_REDIS_DB
    assert redis_target.TEST_REDIS_DB != 0
    assert redis_target.url().endswith(f"/{redis_target.TEST_REDIS_DB}")


def test_check_target_refuses_database_0_and_mismatched_url():
    assert "platform" in redis_target.check_target(db=0, url_db=None)
    assert "must agree" in redis_target.check_target(db=15, url_db="3")
    assert redis_target.check_target(db=15, url_db="15") is None
    assert redis_target.check_target(db=15, url_db=None) is None


def test_session_guard_refuses_to_run_against_database_0():
    """With TEST_REDIS_DB=0 the root conftest's session guard stops the run
    before any test executes (exit code 4)."""
    env = {**os.environ, "TEST_REDIS_DB": "0"}
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider",
         "tests/test_migrations_graph.py"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    out = result.stdout + result.stderr
    assert result.returncode == 4, out
    assert "TEST_REDIS_DB=0" in out
    assert " passed" not in out


def test_no_test_flushes_all_databases_or_hardcodes_a_redis_url():
    allowed = {"tests/redis_target.py", "tests/shared/test_config.py", "tests/test_redis_isolation.py"}
    for path in ROOT.glob("tests/**/*.py"):
        rel = path.relative_to(ROOT).as_posix()
        if rel in allowed:
            continue
        src = path.read_text()
        assert "flushall" not in src, rel
        assert not re.search(r"""from_url\(\s*f?["']redis://""", src), rel
