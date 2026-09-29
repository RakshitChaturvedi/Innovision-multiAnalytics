"""tools/config/apply_config.py against REAL Postgres + Redis (db 15), fake face model."""
import uuid
from pathlib import Path

import cv2
import numpy as np
import pytest
from sqlalchemy import text

from tools.config import apply_config as ac
from tools.config.enrollment import EnrollResult
from tools.platform.common import Camera, ROOT, Reporter

CAM1, CAM2 = str(uuid.uuid4()), str(uuid.uuid4())
CAMERAS = [Camera(CAM1, "Phone 1", {}), Camera(CAM2, "Phone 2", {})]
SQUARE = [[0.1, 0.1], [0.9, 0.1], [0.9, 0.9], [0.1, 0.9]]


class FakeEnroller:
    """Photo pixel value decides: 200 good face, 50 two faces, 10 blurry."""

    def __init__(self):
        self.calls = 0

    def embed(self, image):
        self.calls += 1
        v = int(image.mean())
        if v > 150:
            e = np.random.default_rng(v).normal(size=512).astype(np.float32)
            return EnrollResult(e / np.linalg.norm(e), quality_score=0.9)
        if v > 30:
            return EnrollResult(None, reason="2 faces in the photo (need exactly one)")
        return EnrollResult(None, reason="too_blurry:3.2")


def _photo(path: Path, value: int):
    path.parent.mkdir(parents=True, exist_ok=True)
    ok, buf = cv2.imencode(".jpg", np.full((64, 64, 3), value, np.uint8))
    path.write_bytes(buf.tobytes())


def _cfg(tmp_path, **over):
    data = {
        "cameras": [
            {"name": "Phone 1", "zones": [
                {"name": "Lobby", "type": "monitored", "polygon": SQUARE, "max_headcount": 3,
                 "dwell_threshold_seconds": 15}]},
            {"name": "Phone 2", "zones": [
                {"name": "Server room", "type": "restricted", "polygon": SQUARE,
                 "dwell_threshold_seconds": 10}]},
        ],
        "people": [
            {"name": "Alice", "images_dir": str(tmp_path / "alice"),
             "authorized_zones": ["Phone 2/Server room"], "blocklisted": False},
        ],
    }
    data.update(over)
    return ac.ConfigFile.model_validate(data)


async def _apply(pg_session_factory, redis_client, cfg, enroller=None, **kw):
    r = Reporter()
    plan = await ac.run(cfg, CAMERAS, pg_session_factory, redis_client, r,
                        enroller=enroller or FakeEnroller(), **{"dry_run": False, **kw})
    return r, plan


async def _count(factory, table, where="true", **params):
    async with factory() as s:
        return (await s.execute(text(f"SELECT count(*) FROM {table} WHERE {where}"), params)).scalar()


@pytest.fixture
async def clean(pg_session_factory):
    async with pg_session_factory() as s:
        await s.execute(text("TRUNCATE headcount_breach_events, blocklist CASCADE"))
        await s.commit()
    return pg_session_factory


# ---------------------------------------------------------------- parsing


def test_example_yaml_parses():
    cfg = ac.load_config(ROOT / "tools" / "config" / "config.example.yaml")
    assert [c.name for c in cfg.cameras] == ["Phone 1", "Phone 2"]
    assert cfg.people[0].authorized_zones == ["Phone 2/Server room"]


def test_yaml_errors_are_all_reported(tmp_path):
    p = tmp_path / "c.yaml"
    p.write_text(
        "cameras:\n"
        "  - name: A\n    zones:\n"
        "      - {name: Z, type: kitchen, polygon: [[0,0],[1,1]]}\n"
        "      - {name: Y, type: monitored, polygon: [[0,0],[1.5,0],[1,1]], max_headcount: 0}\n"
        "people:\n  - {name: P, authorized_zones: [nozone]}\n  - {name: P}\n", encoding="utf-8")
    with pytest.raises(ValueError) as exc:
        ac.load_config(p)
    msg = str(exc.value)
    assert "cameras.0.zones.0.type" in msg
    assert "cameras.0.zones.0.polygon" in msg and "at least 3" in msg
    assert "cameras.0.zones.1.polygon" in msg          # 1.5 out of range
    assert "cameras.0.zones.1.max_headcount" in msg
    assert "camera name/zone name" in msg


def test_duplicate_names_rejected():
    with pytest.raises(Exception, match="repeated"):
        ac.ConfigFile.model_validate({"people": [{"name": "A"}, {"name": "A"}]})
    with pytest.raises(Exception, match="repeated"):
        ac.ConfigFile.model_validate({"cameras": [{"name": "C", "zones": [
            {"name": "Z", "type": "safe", "polygon": SQUARE}, {"name": "Z", "type": "safe", "polygon": SQUARE}]}]})


def test_polygon_accepts_xy_dicts_like_zonestore():
    z = ac.ZoneCfg.model_validate({"name": "Z", "type": "safe",
                                   "polygon": [{"x": 0, "y": 0}, {"x": 1, "y": 0}, {"x": 1, "y": 1}]})
    assert z.polygon == [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0]]


# ---------------------------------------------------------------- apply (real Postgres + Redis)


async def test_first_run_creates_second_run_is_unchanged(clean, redis_client, tmp_path):
    _photo(tmp_path / "alice" / "a1.jpg", 200)
    _photo(tmp_path / "alice" / "a2.jpg", 50)     # two faces -> skipped
    pubsub = redis_client.pubsub()
    await pubsub.psubscribe("cache:zone_config:*:invalidate")
    await pubsub.subscribe("cache:enrolled:invalidate")

    enroller = FakeEnroller()
    r, plan = await _apply(clean, redis_client, _cfg(tmp_path), enroller)
    assert r.failed == 0
    assert await _count(clean, "zones") == 2
    assert await _count(clean, "enrolled_persons") == 1
    assert await _count(clean, "face_embeddings", "is_enrollment") == 1
    assert await _count(clean, "zone_authorized_persons") == 1
    assert enroller.calls == 2 and r.warned >= 1   # a2 skipped with its reason

    got = []
    for _ in range(20):
        m = await pubsub.get_message(ignore_subscribe_messages=True, timeout=0.2)
        if m:
            got.append(m["channel"].decode())
    assert f"cache:zone_config:{CAM1}:invalidate" in got
    assert f"cache:zone_config:{CAM2}:invalidate" in got
    assert "cache:enrolled:invalidate" in got
    await pubsub.aclose()

    enroller2 = FakeEnroller()
    r, plan = await _apply(clean, redis_client, _cfg(tmp_path), enroller2)
    assert plan.changes == 0 and r.failed == 0
    assert enroller2.calls == 0                       # nothing re-enrolled, skipped not retried
    assert await _count(clean, "face_embeddings") == 1
    assert {v for v, _ in plan.lines} <= {"UNCHANGED", "SKIP"}
    assert not plan.touched_cameras


async def test_retry_skipped(clean, redis_client, tmp_path):
    _photo(tmp_path / "alice" / "a2.jpg", 50)
    await _apply(clean, redis_client, _cfg(tmp_path))
    enroller = FakeEnroller()
    await _apply(clean, redis_client, _cfg(tmp_path), enroller, retry_skipped=True)
    assert enroller.calls == 1


async def test_dry_run_writes_nothing_and_loads_no_model(clean, redis_client, tmp_path, capsys):
    _photo(tmp_path / "alice" / "a1.jpg", 200)

    class Boom:
        def embed(self, image):
            raise AssertionError("dry run must not run the model")

    r = Reporter()
    plan = await ac.run(_cfg(tmp_path), CAMERAS, clean, redis_client, r, dry_run=True, enroller=Boom())
    out = capsys.readouterr().out
    assert r.failed == 0 and plan.changes == 5  # 2 zones, person, photo, authorization
    for table in ("zones", "enrolled_persons", "face_embeddings", "zone_authorized_persons"):
        assert await _count(clean, table) == 0
    assert "CREATE      zone Phone 1/Lobby" in out and "ENROLL      photo Alice/a1.jpg" in out


async def test_zone_update_and_exact_authorizations(clean, redis_client, tmp_path, capsys):
    (tmp_path / "alice").mkdir()
    await _apply(clean, redis_client, _cfg(tmp_path))
    cfg = _cfg(tmp_path)
    cfg.cameras[0].zones[0].max_headcount = 5
    cfg.people[0].authorized_zones = ["Phone 1/Lobby"]
    capsys.readouterr()
    r, plan = await _apply(clean, redis_client, cfg)
    out = capsys.readouterr().out
    assert "UPDATE      zone Phone 1/Lobby: max_headcount 3 -> 5" in out
    assert "AUTHORIZE   Alice -> Phone 1/Lobby" in out
    assert "REMOVE      authorization Alice -> Phone 2/Server room" in out
    assert plan.touched_cameras == {CAM1}
    async with clean() as s:
        names = (await s.execute(text(
            "SELECT z.name FROM zone_authorized_persons a JOIN zones z ON z.id = a.zone_id"))).scalars().all()
    assert names == ["Lobby"]


async def test_people_not_in_file_are_untouched(clean, redis_client, tmp_path):
    (tmp_path / "alice").mkdir()
    await _apply(clean, redis_client, _cfg(tmp_path))
    bob = str(uuid.uuid4())
    async with clean() as s:
        await s.execute(text("INSERT INTO enrolled_persons (id, name) VALUES (CAST(:i AS uuid), 'Bob')"), {"i": bob})
        await s.execute(text("INSERT INTO zone_authorized_persons SELECT id, CAST(:i AS uuid) FROM zones"), {"i": bob})
        await s.commit()
    await _apply(clean, redis_client, _cfg(tmp_path, people=[]))
    assert await _count(clean, "zone_authorized_persons", "person_id = CAST(:i AS uuid)", i=bob) == 2


async def test_zones_not_in_file_kept_then_pruned(clean, redis_client, tmp_path, capsys):
    (tmp_path / "alice").mkdir()
    await _apply(clean, redis_client, _cfg(tmp_path))
    cfg = _cfg(tmp_path)
    cfg.cameras[1].zones = []
    cfg.people[0].authorized_zones = []
    capsys.readouterr()
    r, plan = await _apply(clean, redis_client, cfg)        # no --prune
    out = capsys.readouterr().out
    assert "KEEP        zone Phone 2/Server room is not in the file" in out
    assert await _count(clean, "zones") == 2

    await _apply(clean, redis_client, _cfg(tmp_path))       # re-authorize Alice
    r = Reporter()
    plan = await ac.run(cfg, CAMERAS, clean, redis_client, r, dry_run=True, prune=True)
    out = capsys.readouterr().out
    assert "DELETE      zone Phone 2/Server room and its 1 authorization(s)" in out
    assert await _count(clean, "zones") == 2                 # dry run

    r, plan = await _apply(clean, redis_client, cfg, prune=True)
    assert r.failed == 0
    assert await _count(clean, "zones") == 1
    assert await _count(clean, "zone_authorized_persons") == 0


async def test_prune_refuses_zone_with_open_breach_or_intrusion(clean, redis_client, tmp_path, capsys):
    (tmp_path / "alice").mkdir()
    await _apply(clean, redis_client, _cfg(tmp_path, people=[]))
    async with clean() as s:
        lobby = (await s.execute(text("SELECT id FROM zones WHERE name='Lobby'"))).scalar()
        server = (await s.execute(text("SELECT id FROM zones WHERE name='Server room'"))).scalar()
        await s.execute(text(
            "INSERT INTO headcount_breach_events (zone_id, camera_id, count, threshold, timestamp, status) "
            "VALUES (:z, :c, 5, 3, now(), 'open')"), {"z": lobby, "c": CAM1})
        await s.execute(text(
            "INSERT INTO intruder_events (zone_id, camera_id, track_id, classification_reason, "
            "first_detected_at, last_seen_at) VALUES (:z, :c, 1, 'unknown', now(), now())"),
            {"z": server, "c": CAM2})
        await s.commit()
    cfg = _cfg(tmp_path, people=[])
    cfg.cameras[0].zones = []
    cfg.cameras[1].zones = []
    capsys.readouterr()
    r, plan = await _apply(clean, redis_client, cfg, prune=True)
    out = capsys.readouterr().out
    assert "open headcount breach" in out and "open intruder event" in out
    assert await _count(clean, "zones") == 2
    assert r.failed == 0 and r.warned == 2
    async with clean() as s:
        await s.execute(text("TRUNCATE intruder_events CASCADE"))
        await s.commit()


async def test_blocklist_follows_the_file(clean, redis_client, tmp_path, capsys):
    (tmp_path / "alice").mkdir()
    cfg = _cfg(tmp_path)
    cfg.people[0].blocklisted = True
    await _apply(clean, redis_client, cfg)
    assert await _count(clean, "blocklist", "reason = 'apply_config'") == 1
    await _apply(clean, redis_client, cfg)                  # idempotent
    assert await _count(clean, "blocklist") == 1
    cfg.people[0].blocklisted = False
    capsys.readouterr()
    await _apply(clean, redis_client, cfg)
    assert "UNBLOCKLIST Alice" in capsys.readouterr().out
    assert await _count(clean, "blocklist") == 0


async def test_unknown_camera_or_zone_ref_fails_and_writes_nothing(clean, redis_client, tmp_path, capsys):
    (tmp_path / "alice").mkdir()
    cfg = _cfg(tmp_path)
    cfg.cameras.append(ac.CameraCfg(name="Phone 9"))
    cfg.people[0].authorized_zones = ["Phone 2/Kitchen"]
    r, plan = await _apply(clean, redis_client, cfg)
    out = capsys.readouterr().out
    assert r.failed == 2
    assert "'Phone 9' is not registered; registered: 'Phone 1', 'Phone 2'" in out
    assert "'Phone 2/Kitchen'" in out and "Nothing was written." in out
    assert await _count(clean, "zones") == 0


async def test_missing_images_dir_fails(clean, redis_client, tmp_path):
    r, plan = await _apply(clean, redis_client, _cfg(tmp_path))  # alice dir does not exist
    assert r.failed == 1
    assert await _count(clean, "enrolled_persons") == 0


async def test_authorized_zone_on_camera_not_in_file(clean, redis_client, tmp_path):
    (tmp_path / "alice").mkdir()
    await _apply(clean, redis_client, _cfg(tmp_path, people=[]))
    cfg = _cfg(tmp_path, cameras=[])          # zones exist only in the database now
    r, plan = await _apply(clean, redis_client, cfg)
    assert r.failed == 0
    assert await _count(clean, "zone_authorized_persons") == 1
