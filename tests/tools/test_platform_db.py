"""tools/platform/platform_db.py against real Postgres (a throwaway database)."""
import asyncio

import pytest
from sqlalchemy.engine import make_url

from tests.tools import fakes
from tools.platform import platform_db
from tools.platform.common import Reporter


def _admin_url():
    from tests.conftest import _test_db_url

    return make_url(_test_db_url())


async def _drop(url, name):
    import asyncpg

    conn = await asyncpg.connect(**platform_db._asyncpg_kwargs(url, "postgres"))
    try:
        await conn.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
    finally:
        await conn.close()


@pytest.fixture
def fresh_db():
    url = _admin_url()
    name = "innovision_analytics_tooltest"
    try:
        asyncio.run(_drop(url, name))
    except Exception as exc:
        from tests import redis_target

        redis_target.unavailable(f"no Postgres: {exc}")
    yield url.set(database=name).render_as_string(hide_password=False), name
    asyncio.run(_drop(url, name))


def test_creates_database_enables_vector_and_migrates_idempotently(fresh_db, capsys):
    db_url, name = fresh_db
    r = Reporter()
    platform_db.setup(db_url, r, expected_db=name)
    out = capsys.readouterr().out
    assert r.failed == 0, out
    assert f"PASS database: {name} created" in out
    assert "PASS pgvector" in out and "PASS alembic: heads:" in out
    assert "postgres:postgres@" not in out

    r = Reporter()  # second run: nothing to create, still PASS
    platform_db.setup(db_url, r, expected_db=name)
    out = capsys.readouterr().out
    assert r.failed == 0 and f"PASS database: {name} exists" in out


@pytest.mark.parametrize("db", ["innovision_platform", "postgres", "other"])
def test_refuses_any_other_database(db, capsys):
    r = Reporter()
    platform_db.setup(fakes.userinfo_url("postgresql+asyncpg", "127.0.0.1:1", f"/{db}", user="u", label="db"), r)
    out = capsys.readouterr().out
    assert r.failed == 1 and "innovision_analytics" in out
    assert "PASS" not in out  # refused before connecting anywhere


def test_unreachable_postgres_fails_with_fix(capsys):
    r = Reporter()
    platform_db.setup(fakes.userinfo_url("postgresql+asyncpg", "127.0.0.1:1", "/innovision_analytics",
                                         user="u", label="db"), r)
    out = capsys.readouterr().out
    assert r.failed == 1 and "FIX:" in out and fakes.MARKER not in out
