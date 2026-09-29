"""Install a FAKE `ultralytics` when the real package is missing.

CI/dev boxes without model weights or torch can then import the detection
service (detector/tracker modules import ultralytics at module level) and test
it with a fake YOLO and an IoU-based stand-in for ByteTrack. When the real
package is installed nothing is patched.
"""
import importlib.util
import sys
import types

import numpy as np


class _FakeYOLO:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, frames, **kwargs):  # never used: tests inject a detector
        raise RuntimeError("fake YOLO must not run inference")


class _FakeBYTETracker:
    """Greedy IoU tracker: new local ids from 1, dropped after track_buffer misses."""

    def __init__(self, args, frame_rate=30):
        self._buffer = int(getattr(args, "track_buffer", 30))
        self._next = 1
        self._tracks = []  # dicts: id, box, missed

    @staticmethod
    def _iou(a, b):
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
        return inter / union if union > 0 else 0.0

    def update(self, results, img=None):
        rows = []
        used = set()
        for box, conf, cls in zip(results.xyxy, results.conf, results.cls):
            best, best_iou = None, 0.3
            for i, t in enumerate(self._tracks):
                if i in used:
                    continue
                iou = self._iou(box, t["box"])
                if iou > best_iou:
                    best, best_iou = i, iou
            if best is None:
                self._tracks.append({"id": self._next, "box": box, "missed": 0})
                best = len(self._tracks) - 1
                self._next += 1
            used.add(best)
            self._tracks[best].update(box=box, missed=0)
            rows.append([*box, self._tracks[best]["id"], conf, cls])
        for i, t in enumerate(self._tracks):
            if i not in used:
                t["missed"] += 1
        self._tracks = [t for t in self._tracks if t["missed"] <= self._buffer]
        return np.array(rows, dtype=np.float32) if rows else np.zeros((0, 7), np.float32)


if importlib.util.find_spec("ultralytics") is None:
    ultralytics = types.ModuleType("ultralytics")
    ultralytics.YOLO = _FakeYOLO
    trackers = types.ModuleType("ultralytics.trackers")
    trackers.BYTETracker = _FakeBYTETracker
    ultralytics.trackers = trackers
    sys.modules["ultralytics"] = ultralytics
    sys.modules["ultralytics.trackers"] = trackers


# ---------------------------------------------------------------------------
# Real services, shared by every test that needs them (event_processing,
# integration). Unreachable -> skip, or FAIL with REQUIRE_REAL_SERVICES=1.
#   Redis:    tests/redis_target.py (TEST_REDIS_URL host/port, TEST_REDIS_DB)
#   Postgres: TEST_DATABASE_URL (default ...127.0.0.1:5432/innovision_analytics_test),
#             dropped/recreated and migrated with alembic once per run.
# ---------------------------------------------------------------------------
import os  # noqa: E402
import subprocess  # noqa: E402
from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
import pytest_asyncio  # noqa: E402

from tests import redis_target  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

TABLES = (
    "zone_events, intruder_events, recognition_events, face_embeddings, "
    "enrolled_persons, blocklist, zone_authorized_persons, zones"
)


@pytest.fixture(scope="session", autouse=True)
def _refuse_platform_redis_database():
    """Tests never run against Redis database 0 (the platform's)."""
    problem = redis_target.check_target()
    if problem:
        pytest.exit(problem, returncode=4)


@pytest_asyncio.fixture
async def redis_client():
    import redis.asyncio as aioredis

    client = aioredis.from_url(redis_target.url())
    try:
        await client.ping()
    except Exception:
        await client.aclose()
        redis_target.unavailable(f"no Redis at {redis_target.url()}")
    await client.flushdb()  # the dedicated test database only
    yield client
    await client.flushdb()
    await client.aclose()


def _test_db_url() -> str:
    return os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/innovision_analytics_test",
    )


def _recreate_database(url: str) -> None:
    from sqlalchemy import create_engine, make_url, text

    parsed = make_url(url)
    try:
        import psycopg2  # noqa: F401

        admin = create_engine(
            parsed.set(drivername="postgresql+psycopg2", database="postgres"),
            isolation_level="AUTOCOMMIT",
        )
        with admin.connect() as conn:
            conn.execute(text(f'DROP DATABASE IF EXISTS "{parsed.database}" WITH (FORCE)'))
            conn.execute(text(f'CREATE DATABASE "{parsed.database}"'))
        admin.dispose()
    except Exception as exc:
        redis_target.unavailable(f"no Postgres at {url}: {exc}")


@pytest.fixture(scope="session")
def pg_url():
    import sys

    url = _test_db_url()
    _recreate_database(url)
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "-c", "migrations/alembic.ini", "upgrade", "head"],
        cwd=ROOT, env={**os.environ, "DATABASE_URL": url}, capture_output=True, text=True,
    )
    if result.returncode != 0:
        pytest.fail(f"alembic upgrade failed:\n{result.stderr[-2000:]}")
    return url


@pytest.fixture(scope="session")
def pg_scratch_url():
    """An EMPTY sibling of the test database (TEST_DATABASE_URL + '_scratch')
    for tests that drop the schema or walk the migrations themselves."""
    from sqlalchemy import make_url

    parsed = make_url(_test_db_url())
    url = parsed.set(database=f"{parsed.database}_scratch").render_as_string(hide_password=False)
    _recreate_database(url)
    return url


@pytest_asyncio.fixture
async def pg_session_factory(pg_url):
    from sqlalchemy import text
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(pg_url, poolclass=NullPool)
    factory = async_sessionmaker(engine, expire_on_commit=False)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {TABLES} CASCADE"))
    yield factory
    await engine.dispose()
