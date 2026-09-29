"""tools/platform/diagnostics.py: zip contents, masking, no binaries; streams on REAL Redis."""
import zipfile

from tests import redis_target
from tests.tools import fakes
from tools.platform import diagnostics as dg
from tools.platform.common import Reporter



def _setup(tmp_path):
    logs = tmp_path / "logs"
    logs.mkdir()
    (logs / "detection.log").write_text(
        "".join(f"line {i}\n" for i in range(800))
        + "frame_redis_miss ref " + fakes.userinfo_url("rtsp", "10.0.0.9", "/live", user="admin", label="cam")
        + "\nAuthorization: Bearer " + fakes.fake("jwt") + ".x.y\n", encoding="utf-8")
    (logs / "photo.jpg").write_bytes(b"\xff\xd8binary")
    (logs / "detection.pid").write_text("123")
    usecase = tmp_path / "usecase.env"
    usecase.write_text("\n".join([
        "REDIS_HOST=localhost",
        fakes.assignment("INTERNAL_SERVICE_TOKEN", "token"),
        fakes.assignment("MINIO_SECRET_KEY", "minio"),
        "DATABASE_URL=" + fakes.userinfo_url("postgresql+asyncpg", "localhost:5432", "/innovision_analytics",
                                             user="analytics", label="db"),
    ]) + "\n", encoding="utf-8")
    platform = tmp_path / "platform"
    (platform / "infra").mkdir(parents=True)
    (platform / "infra" / "docker-compose.yml").write_text(
        "services:\n  ingestion: {}\n  camera-registry: {}\n  alert_management: {}\n", encoding="utf-8")
    (platform / ".env").write_text(
        fakes.assignment("JWT_SECRET", "jwt") + "\n" + fakes.assignment("INTERNAL_SERVICE_TOKEN", "token")
        + "\nPOSTGRES_USER=platform\n", encoding="utf-8")
    return logs, usecase, platform


def fake_runner(calls):
    def runner(cmd, timeout=120):
        calls.append(cmd)
        if cmd[:2] == ["docker", "compose"] and "logs" in cmd:
            return f"{cmd[-1]} | connecting to " + fakes.userinfo_url("redis", "redis:6379", user="", label="redis") + "\n"
        return "ok " + fakes.assignment("INTERNAL_SERVICE_TOKEN", "token") + "\n"
    return runner


def test_zip_has_every_part_masked_and_no_binaries(tmp_path):
    logs, usecase, platform = _setup(tmp_path)
    calls = []
    out = tmp_path / "d.zip"

    async def streams(env):
        return "frames:x len=3\n  last 1-0: bad payload " + fakes.assignment("token", "stream") + "\n"

    r = Reporter()
    dg.collect(r, out, platform, runner=fake_runner(calls), env={}, streams=streams,
               health=lambda: {"detection": '{"status": "ok"}'}, logs_dir=logs, usecase_env=usecase)
    assert r.failed == 0
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
        body = {n: z.read(n).decode() for n in names}
    assert names == {"logs/detection.log", "check_integration.txt", "health/detection.json", "streams.txt",
                     "alembic.txt", "env/usecase.env", "docker_compose_ps.txt",
                     "platform_logs/ingestion.txt", "platform_logs/camera_registry.txt",
                     "platform_logs/alert_management.txt", "env/platform.env"}
    everything = "\n".join(body.values())
    assert fakes.MARKER not in everything
    assert "rtsp://***@10.0.0.9/live" in body["logs/detection.log"]
    assert "INTERNAL_SERVICE_TOKEN=***" in body["env/usecase.env"]
    assert "JWT_SECRET=***" in body["env/platform.env"]
    assert len(body["logs/detection.log"].splitlines()) == dg.LOG_LINES
    assert "10.0.0.9" in body["logs/detection.log"]
    assert "REDIS_HOST=localhost" in body["env/usecase.env"]
    assert "POSTGRES_USER=platform" in body["env/platform.env"]
    assert "camera-registry" in body["platform_logs/camera_registry.txt"]
    tails = [c for c in calls if "logs" in c]
    assert all(c[c.index("--tail") + 1] == "300" for c in tails) and len(tails) == 3


def test_without_platform_path_warns(tmp_path):
    logs, usecase, _ = _setup(tmp_path)

    async def streams(env):
        return ""

    r = Reporter()
    dg.collect(r, tmp_path / "d.zip", None, runner=fake_runner([]), env={}, streams=streams,
               health=lambda: {}, logs_dir=logs, usecase_env=usecase)
    assert r.warned == 1 and r.failed == 0


def test_bundle_refuses_non_text(tmp_path):
    b = dg.Bundle(tmp_path / "x.zip")
    try:
        import pytest

        with pytest.raises(ValueError):
            b.add("snapshot.png", "x")
    finally:
        b.close()


async def test_streams_report_on_real_redis(redis_client):
    await redis_client.xadd("events:zone", {"data": "{}"}, id="1000-0")
    await redis_client.xgroup_create("events:zone", "intruder_group", id="0")
    await redis_client.xreadgroup("intruder_group", "c", {"events:zone": ">"})
    await redis_client.xadd("events:zone:dlq", {"data": "x", "error": "bad payload"})
    env = {"REDIS_HOST": redis_target.HOST, "REDIS_PORT": redis_target.PORT,
           "REDIS_DB": redis_target.TEST_REDIS_DB}
    text = await dg.streams_report(env)
    assert "events:zone len=1" in text
    assert "group intruder_group pending=1" in text and "oldest_pending_age_s=" in text
    assert "events:zone:dlq len=1" in text and "bad payload" in text
