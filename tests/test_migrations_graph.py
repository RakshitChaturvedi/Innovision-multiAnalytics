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


def _added_columns(path: Path) -> set[tuple[str, str]]:
    """(table, column) pairs added by a migration file, from raw SQL and op.add_column."""
    import re

    src = path.read_text()
    up = src.split("def downgrade", 1)[0]
    found = set()
    for m in re.finditer(r'op\.add_column\(\s*"(\w+)",\s*sa\.Column\(\s*"(\w+)"', up):
        found.add((m.group(1), m.group(2)))
    for m in re.finditer(r"ALTER TABLE\s+(\w+)(.*?)(?:\"\"\"|\")", up, re.S | re.I):
        for col in re.finditer(r"ADD COLUMN\s+(\w+)", m.group(2), re.I):
            found.add((m.group(1), col.group(1)))
    return found


def test_no_two_migrations_add_the_same_column():
    seen: dict[tuple[str, str], str] = {}
    for path in sorted((ROOT / "migrations" / "versions").glob("*.py")):
        for key in _added_columns(path):
            assert key not in seen, f"{key} added by both {seen[key]} and {path.name}"
            seen[key] = path.name


def test_0007_sits_on_top_of_0006_reliability(script):
    rev = script.get_revision("0007_headcount_breach_state")
    assert rev.down_revision == "0006_reliability"


def test_single_head_every_revision_leads_to_it(script):
    """Head-agnostic: whichever migration is newest, there is exactly one
    head, every revision is an ancestor of it and nothing but the head is a
    leaf, so a new migration must be chained on the real current head."""
    (head,) = script.get_heads()
    everything = {r.revision for r in script.walk_revisions()}
    assert {r.revision for r in script.walk_revisions(base="base", head=head)} == everything
    leaves = {r for r in everything if not script.get_revision(r).nextrev}
    assert leaves == {head}


def test_0010_blur_recalibration_sits_on_top_of_0009(script):
    rev = script.get_revision("0010_blur_recalibration")
    assert rev.down_revision == "0009_intruder_candidate"
