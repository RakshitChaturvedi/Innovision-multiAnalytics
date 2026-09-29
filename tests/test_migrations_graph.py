"""Alembic graph sanity: one head, no dangling parents, separate version table."""
from pathlib import Path

import pytest

pytest.importorskip("alembic")
pytest.importorskip("pgvector")  # 0003_recognition imports pgvector.sqlalchemy

from alembic.config import Config  # noqa: E402
from alembic.script import ScriptDirectory  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def script():
    cfg = Config(str(ROOT / "migrations" / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "migrations"))
    return ScriptDirectory.from_config(cfg)


def test_exactly_one_head(script):
    # Name-agnostic: other branches may add migrations on top.
    assert len(script.get_heads()) == 1


def test_0005_headcount_is_directly_below_head(script):
    assert script.get_revision("0006_reliability").down_revision == "0005_headcount"


def test_0004_intruder_parents_exist(script):
    rev = script.get_revision("0004_intruder")
    assert set(rev.down_revision) == {"0003", "0003_zone_monitor"}
    for parent in rev.down_revision:
        assert script.get_revision(parent) is not None


def test_whole_graph_walks_to_base(script):
    revs = list(script.walk_revisions())
    assert {r.revision for r in revs} >= {"0001", "0005_headcount", "0006_reliability"}


def test_env_uses_analytics_version_table_in_both_modes():
    env = (ROOT / "migrations" / "env.py").read_text()
    assert 'VERSION_TABLE = "alembic_version_analytics"' in env
    assert env.count("version_table=VERSION_TABLE") == 2
