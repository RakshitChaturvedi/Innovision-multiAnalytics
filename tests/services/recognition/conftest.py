"""Fixtures for the recognition service tests.

- `redis`: a REAL local redis-server (skipped if the binary is missing).
- FakeDB / FakeFace: fakes. There is no Postgres and no InsightFace here.
"""
import shutil
import socket
import subprocess
import sys
import time
import types
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime

import cv2
import numpy as np
import pytest
import redis.asyncio as aioredis

# services/recognition/src/model_loader.py imports insightface at module level
# (model loading is not to be touched). Provide an import stub if the package
# is not installed; the model itself is always a fake in these tests.
try:  # pragma: no cover
    import insightface  # noqa: F401
except ImportError:
    pkg = types.ModuleType("insightface")
    app = types.ModuleType("insightface.app")
    common = types.ModuleType("insightface.app.common")
    app.FaceAnalysis = type("FaceAnalysis", (), {})
    common.Face = type("Face", (), {})
    app.common = common
    pkg.app = app
    sys.modules.update({"insightface": pkg, "insightface.app": app, "insightface.app.common": common})


@pytest.fixture(scope="session")
def redis_port():
    if shutil.which("redis-server") is None:
        pytest.skip("redis-server not installed")
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
    client = aioredis.from_url(f"redis://127.0.0.1:{redis_port}", decode_responses=False)
    await client.flushall()
    yield client
    await client.aclose()


# ----------------------------------------------------------------- fake model


@dataclass
class FakeFace:
    bbox: tuple = (5.0, 5.0, 60.0, 60.0)
    det_score: float = 0.99
    pose: tuple = (0.0, 0.0, 0.0)  # InsightFace order: [pitch, yaw, roll]
    embedding: np.ndarray = field(default_factory=lambda: _unit(0))


class FrameFaceLoader:
    """Fake model whose faces are placed in FRAME pixels.

    `attach(consumer)` wraps the consumer's FaceCropper so the loader knows
    where the current crop sits; it then returns, in crop-local pixels, every
    face whose center lies inside that crop, like the real model would.
    Implements both the old single-face API and `detect_faces`.
    """

    def __init__(self, faces: list[FakeFace]):
        self.faces = faces  # bbox in frame pixels
        self.origin = (0, 0)
        self.crop_calls = 0

    def attach(self, consumer) -> "FrameFaceLoader":
        crop = consumer._cropper.crop

        def recording_crop(frame, bbox):
            result = crop(frame, bbox)
            if result is not None:
                self.origin = (result.x1, result.y1)
                self.shape = result.image.shape[:2]
            return result

        consumer._cropper.crop = recording_crop
        consumer._model_loader = self
        return self

    def detect_faces(self, crop):
        self.crop_calls += 1
        ox, oy = self.origin
        h, w = crop.shape[:2]
        out = []
        for f in self.faces:
            cx, cy = (f.bbox[0] + f.bbox[2]) / 2 - ox, (f.bbox[1] + f.bbox[3]) / 2 - oy
            if 0 <= cx < w and 0 <= cy < h:
                x1, y1, x2, y2 = f.bbox
                out.append(FakeFace(bbox=(x1 - ox, y1 - oy, x2 - ox, y2 - oy),
                                    det_score=f.det_score, pose=f.pose, embedding=f.embedding))
        return out

    def detect_best_face(self, crop):  # pre-fix API
        faces = self.detect_faces(crop)
        return max(faces, key=lambda f: f.det_score) if faces else None


def face_at(cx: float, cy: float, frame_w: int, frame_h: int, embedding, det_score=0.9, size=24.0):
    """FakeFace centered at normalized (cx, cy), bbox in frame pixels."""
    x, y = cx * frame_w, cy * frame_h
    return FakeFace(bbox=(x - size / 2, y - size / 2, x + size / 2, y + size / 2),
                    det_score=det_score, embedding=embedding)


def person_track(track_id: int, x1: float, y1: float, x2: float, y2: float, face_ratio=0.35) -> dict:
    """Track dict as detection emits it: face_bbox = full-width top part of the box."""
    box = {"x1": x1, "y1": y1, "x2": x2, "y2": y2}
    face = {"x1": x1, "y1": y1, "x2": x2, "y2": y1 + (y2 - y1) * face_ratio}
    return {"track_id": track_id, "bbox": box, "confidence": 0.9, "class_label": "person",
            "has_face": True, "face_bbox": face}


def _unit(i: int, dim: int = 512) -> np.ndarray:
    v = np.zeros(dim, dtype=np.float32)
    v[i] = 1.0
    return v


unit = _unit


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


def jpeg(w=320, h=240) -> bytes:
    rng = np.random.default_rng(1)
    img = rng.integers(0, 255, (h, w, 3), dtype=np.uint8)
    ok, buf = cv2.imencode(".jpg", img)
    assert ok
    return buf.tobytes()


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
