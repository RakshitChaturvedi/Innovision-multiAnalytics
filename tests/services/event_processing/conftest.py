"""Shared fixtures for event_processing tests.

Real services are used when reachable, otherwise the tests SKIP (never
silently pass on a fake):
  - Redis:    TEST_REDIS_URL     (default redis://127.0.0.1:6379/15)
  - Postgres: TEST_DATABASE_URL  (default ...127.0.0.1:5432/innovision_analytics_test)
    The database is dropped/recreated and migrated with alembic once per run.
"""
import os
import subprocess
import sys
from pathlib import Path

import pytest
import pytest_asyncio
import redis.asyncio as aioredis
from sqlalchemy import make_url, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

ROOT = Path(__file__).resolve().parents[3]

TABLES = (
    "zone_events, intruder_events, recognition_events, face_embeddings, "
    "enrolled_persons, blocklist, zone_authorized_persons, zones"
)


@pytest_asyncio.fixture
async def redis_client():
    url = os.environ.get("TEST_REDIS_URL", "redis://127.0.0.1:6379/15")
    client = aioredis.from_url(url)
    try:
        await client.ping()
    except Exception:
        pytest.skip(f"no Redis at {url}")
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


@pytest.fixture(scope="session")
def pg_url():
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/innovision_analytics_test",
    )
    parsed = make_url(url)
    sync_admin = parsed.set(drivername="postgresql+psycopg2", database="postgres")

    try:
        import psycopg2  # noqa: F401
        from sqlalchemy import create_engine

        admin = create_engine(sync_admin, isolation_level="AUTOCOMMIT")
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
        admin.dispose()
    except Exception as exc:
        pytest.skip(f"no Postgres at {url}: {exc}")

    env = {**os.environ, "DATABASE_URL": url}
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"],
        cwd=ROOT, env=env, capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed:\n{result.stderr[-2000:]}")
    return url


@pytest_asyncio.fixture
async def pg_session_factory(pg_url):
    engine = create_async_engine(pg_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    yield factory
    await engine.dispose()
