"""Fixtures for the recognition service tests.

- `redis`: a REAL local redis-server (skipped if the binary is missing).
- FakeDB / FakeFace: fakes. There is no Postgres and no InsightFace here.
"""
import shutil
import socket
import subprocess
import time
import uuid
from datetime import UTC, datetime

import pytest
import redis.asyncio as aioredis

from tests import redis_target

from tests.recognition_fakes import (  # noqa: F401  (re-exported for the tests)
    FakeFace,
    FrameFaceLoader,
    face_at,
    jpeg,
    person_track,
    unit,
)


@pytest.fixture(scope="session")
def redis_port():
    if shutil.which("redis-server") is None:
        redis_target.unavailable("redis-server not installed")
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
    proc = subprocess.Popen(
        ["redis-server", "--port", str(port), "--save", "", "--appendonly", "no"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", port), timeout=0.2).close()
            break
        except OSError:
            time.sleep(0.1)
    yield port
    proc.terminate()
    proc.wait(timeout=5)


@pytest.fixture
async def redis(redis_port):
    client = aioredis.from_url(redis_target.url("127.0.0.1", redis_port), decode_responses=False)
    await client.flushdb()  # the dedicated test database only
    yield client
    await client.aclose()


# ------------------------------------------------------------------- fake DB


class FakeResult:
    def __init__(self, rows=None, rowcount=1):
        self._rows = rows or []
        self.rowcount = rowcount

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class FakeDB:
    """Records statements. Statements become 'committed' only if the
    `session.begin()` block exits without an exception, like a real txn."""

    def __init__(self):
        self.committed: list[tuple[str, dict]] = []
        self.rolled_back: list[tuple[str, dict]] = []
        self.fail_on: str | None = None  # substring of a statement that raises
        self.seen_ids: set[tuple[str, str]] = set()  # emulates PK + ON CONFLICT DO NOTHING
        self.select_rows: list = []
        self.select_error: Exception | None = None
        self.select_calls = 0

    def __call__(self):
        return FakeSession(self)

    def tables(self, table: str) -> list[dict]:
        return [p for sql, p in self.committed if f"INSERT INTO {table}" in sql]


class _Txn:
    def __init__(self, session):
        self.s = session

    async def __aenter__(self):
        self.s.pending = []
        return self.s

    async def __aexit__(self, exc_type, *a):
        (self.s.db.rolled_back if exc_type else self.s.db.committed).extend(self.s.pending)
        self.s.pending = []
        return False


class FakeSession:
    def __init__(self, db: FakeDB):
        self.db = db
        self.pending: list = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def begin(self):
        return _Txn(self)

    async def execute(self, stmt, params=None):
        sql = " ".join(str(stmt).split())
        params = params or {}
        if self.db.fail_on and self.db.fail_on in sql:
            raise RuntimeError(f"boom: {self.db.fail_on}")
        if sql.lstrip().upper().startswith("SELECT"):
            self.db.select_calls += 1
            if self.db.select_error:
                raise self.db.select_error
            return FakeResult(rows=self.db.select_rows)
        if "ON CONFLICT" in sql and "id" in params:
            key = (sql.split("INSERT INTO ")[1].split()[0], params["id"])
            if key in self.db.seen_ids:
                return FakeResult(rowcount=0)
            self.db.seen_ids.add(key)
        self.pending.append((sql, params))
        return FakeResult()


# ------------------------------------------------------------- event helpers


def detection_event(camera_id=None, frame_seq=1, provider="redis", ref=None, tracks=None, ts=None) -> dict:
    camera_id = camera_id or uuid.uuid4()
    face = {"x1": 0.1, "y1": 0.1, "x2": 0.6, "y2": 0.7}
    return {
        "event_id": str(uuid.uuid4()),
        "camera_id": str(camera_id),
        "frame_event_id": str(uuid.uuid4()),
        "timestamp": (ts or datetime(2026, 1, 1, 12, 0, 0, tzinfo=UTC)).isoformat(),
        "frame_reference": ref or f"frame:{camera_id}:{frame_seq}",
        "frame_provider": provider,
        "frame_seq": frame_seq,
        "frame_shape": [240, 320],
        "inference_latency_ms": 1.0,
        "tracks": tracks if tracks is not None else [{
            "track_id": 7, "bbox": face, "confidence": 0.9, "class_label": "person",
            "has_face": True, "face_bbox": face,
        }],
    }
