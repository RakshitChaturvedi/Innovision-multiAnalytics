"""tools/platform/services.py: real background processes, real /health (Redis db 15 + Postgres)."""
import socket
import sys
import time

import pytest

from tests import redis_target
from tests.tools.registry_stub import SERVICE_TOKEN
from tools.platform import services as sv
from tools.platform.common import Reporter


def _free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture
def fake_env(monkeypatch, pg_url, redis_client):
    port = _free_port()
    monkeypatch.setenv("FAKE_PORT", str(port))
    monkeypatch.setenv("FAKE_REDIS_URL", redis_target.url())
    monkeypatch.setenv("FAKE_DB_URL", pg_url)
    return sv.Service("fake", "tests.tools.fake_service", port)


def test_up_status_down_graceful(fake_env, tmp_path, capsys):
    paths = sv.Paths(tmp_path / "logs")
    r = Reporter()
    sv.start(r, paths, [fake_env], wait_s=20)
    out = capsys.readouterr().out
    assert r.failed == 0, out
    pid = sv.read_pid(paths.pid(fake_env))
    assert pid and sv.pid_alive(pid)
    assert f"/health on :{fake_env.health_port} ok" in out

    r = Reporter()  # second up: already running, no second process
    sv.start(r, paths, [fake_env], wait_s=5)
    assert "already running" in capsys.readouterr().out
    assert sv.read_pid(paths.pid(fake_env)) == pid

    r = Reporter()
    sv.stop(r, paths, [fake_env], timeout_s=15)
    out = capsys.readouterr().out
    assert r.failed == 0 and r.warned == 0, out
    assert "stopped gracefully" in out
    assert not sv.pid_alive(pid)
    assert not paths.pid(fake_env).exists() and not paths.stop(fake_env).exists()
    assert "fake_service_stopped" in paths.log(fake_env).read_text()


def test_stop_file_alone_stops_the_service(fake_env, tmp_path, monkeypatch):
    """The Windows path: no signal at all, only the stop file."""
    paths = sv.Paths(tmp_path / "logs")
    sv.start(Reporter(), paths, [fake_env], wait_s=20)
    pid = sv.read_pid(paths.pid(fake_env))
    paths.stop(fake_env).write_text("stop")
    deadline = time.monotonic() + 10
    while sv.pid_alive(pid) and time.monotonic() < deadline:
        time.sleep(0.2)
    assert not sv.pid_alive(pid)


def test_crash_on_startup_is_reported_with_log(fake_env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_MODE", "crash")
    paths = sv.Paths(tmp_path / "logs")
    r = Reporter()
    sv.start(r, paths, [fake_env], wait_s=10)
    out = capsys.readouterr().out
    assert r.failed == 1 and "model pack missing" in out
    assert not paths.pid(fake_env).exists()


def test_stubborn_process_is_force_killed(fake_env, tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("FAKE_MODE", "stubborn")
    paths = sv.Paths(tmp_path / "logs")
    sv.start(Reporter(), paths, [fake_env], wait_s=1)   # no /health: FAIL, but running
    pid = sv.read_pid(paths.pid(fake_env))
    assert sv.pid_alive(pid)
    capsys.readouterr()
    r = Reporter()
    sv.stop(r, paths, [fake_env], timeout_s=1)
    out = capsys.readouterr().out
    assert r.warned == 1 and "force-killed" in out
    assert not sv.pid_alive(pid)


def test_down_when_nothing_runs(tmp_path):
    r = Reporter()
    sv.stop(r, sv.Paths(tmp_path / "logs"), [sv.SERVICES[0]])
    assert r.failed == 0


def test_stale_pid_file_is_replaced(fake_env, tmp_path):
    paths = sv.Paths(tmp_path / "logs")
    paths.logs.mkdir(parents=True)
    paths.pid(fake_env).write_text("999999")
    r = Reporter()
    sv.start(r, paths, [fake_env], wait_s=20)
    assert r.failed == 0 and sv.read_pid(paths.pid(fake_env)) != 999999
    sv.stop(Reporter(), paths, [fake_env], timeout_s=15)


# ---------------------------------------------------------------- preflight


def _env(registry, **over):
    env = {"REDIS_HOST": redis_target.HOST, "REDIS_PORT": str(redis_target.PORT),
           "MINIO_ENDPOINT": f"{redis_target.HOST}:{redis_target.PORT}",   # any open port
           "CAMERA_REGISTRY_URL": registry.url, "INTERNAL_SERVICE_TOKEN": SERVICE_TOKEN,
           "SOURCE_UC": "uc1"}
    env.update(over)
    return env


def test_preflight_lists_cameras(registry, monkeypatch, capsys):
    monkeypatch.setattr(sv, "check_stubs", lambda r, p: r.ok("stubs", "none"))
    cam = registry.add("Phone 1")
    registry.add("Other", use_cases=["uc2"])
    r = Reporter()
    assert sv.preflight(r, _env(registry), None) == [cam["id"]]
    out = capsys.readouterr().out
    assert r.failed == 0 and f"camera 'Phone 1' {cam['id']}" in out and "Other" not in out


def test_preflight_fails_without_cameras_or_with_health_port(registry, monkeypatch):
    monkeypatch.setattr(sv, "check_stubs", lambda r, p: r.ok("stubs", "none"))
    r = Reporter()
    assert sv.preflight(r, _env(registry, HEALTH_PORT="9000"), None) == []
    assert r.failed == 2   # HEALTH_PORT + no camera


def test_up_refuses_to_start_on_preflight_fail(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(sv, "load_env", lambda: {"INTERNAL_SERVICE_TOKEN": ""})
    monkeypatch.setattr(sv, "preflight", lambda r, env, p: [])
    monkeypatch.setattr(sv, "start", lambda *a, **k: pytest.fail("must not start"))
    assert sv.main(["up"]) == 1
    assert "Not starting" in capsys.readouterr().out
