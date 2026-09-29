"""Startup smoke tests: run the REAL `python -m services.<svc>.main` (and the
real launcher `tools.platform.services up/status/down`) against real Redis
(TEST_REDIS_DB) and Postgres, with fake models (tests/fake_models, only active
with INNOVISION_FAKE_MODELS=1). Each service must serve GET /health 200, then
exit cleanly on SIGTERM / `down`.

Regression: detection called settings.redis_url() on a @property -> TypeError
at startup; no test ran a real main(), so it shipped.
"""
import os
import signal
import socket
import subprocess
import sys
import time
import urllib.request
import uuid
from pathlib import Path

import pytest

from tests import redis_target

ROOT = Path(__file__).resolve().parents[2]
FAKE_MODELS = ROOT / "tests" / "fake_models"
SERVICES = ("detection", "recognition", "event_processing")
HEALTH_TIMEOUT_S = 45
EXIT_TIMEOUT_S = 20


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _require_redis() -> None:
    import redis

    try:
        redis.Redis.from_url(redis_target.url(), socket_timeout=3).ping()
    except Exception as exc:
        redis_target.unavailable(f"no Redis at {redis_target.url()}: {exc}")


def _env(pg_url: str, tmp_path: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if k != "HEALTH_PORT"}
    env.update({
        "PYTHONPATH": os.pathsep.join([str(FAKE_MODELS), str(ROOT)]),
        "INNOVISION_FAKE_MODELS": "1",
        "PYTHONUNBUFFERED": "1",
        # every service config family (shared Settings + DetectionSettings)
        "REDIS_HOST": redis_target.HOST,
        "REDIS_PORT": str(redis_target.PORT),
        "REDIS_DB": str(redis_target.TEST_REDIS_DB),
        "DATABASE_URL": pg_url,
        "DETECTION_CAMERA_IDS": str(uuid.uuid4()),  # static list: no registry needed
        "MINIO_ENDPOINT": "",
        "YOLOV11M_PATH": str(tmp_path / "fake.pt"),
        "MODEL_ROOT": str(tmp_path / "models"),
    })
    return env


def _health(port: int) -> int | None:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code
    except OSError:
        return None


@pytest.mark.parametrize("service", SERVICES)
def test_service_starts_serves_health_and_stops_on_sigterm(service, pg_url, tmp_path):
    _require_redis()
    port = _free_port()
    env = {**_env(pg_url, tmp_path), "HEALTH_PORT": str(port)}
    log = tmp_path / f"{service}.log"
    with open(log, "wb") as out:
        proc = subprocess.Popen([sys.executable, "-m", f"services.{service}.main"], cwd=tmp_path,
                                env=env, stdout=out, stderr=subprocess.STDOUT)
    try:
        deadline = time.monotonic() + HEALTH_TIMEOUT_S
        status = None
        while time.monotonic() < deadline and proc.poll() is None:
            status = _health(port)
            if status == 200:
                break
            time.sleep(0.5)
        output = log.read_text(errors="replace")
        assert proc.poll() is None, f"{service} exited during startup:\n{output[-3000:]}"
        assert status == 200, f"{service} /health={status} after {HEALTH_TIMEOUT_S}s:\n{output[-3000:]}"

        proc.send_signal(signal.SIGTERM)
        code = proc.wait(timeout=EXIT_TIMEOUT_S)
        output = log.read_text(errors="replace")
        assert code == 0, f"{service} exit code {code}:\n{output[-3000:]}"
        assert "Traceback" not in output, output[-3000:]
    finally:
        if proc.poll() is None:
            proc.kill()
            proc.wait()


def test_launcher_up_status_down(pg_url, tmp_path, monkeypatch, capsys):
    """The exact path used on the PC: `tools.platform.services` main() with
    up / status / down, default ports 8081-8083, real child processes. Only the
    platform preflight (registry, token) is replaced; there is no platform here."""
    _require_redis()
    from tools.platform import services as launcher

    busy = [s.health_port for s in launcher.SERVICES if _health(s.health_port) is not None
            or socket.socket().connect_ex(("127.0.0.1", s.health_port)) == 0]
    if busy:
        pytest.fail(f"ports {busy} already in use (services running?); stop them first")
    for key, value in _env(pg_url, tmp_path).items():
        monkeypatch.setenv(key, value)
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    logs = tmp_path / "logs"
    monkeypatch.setenv("INNOVISION_LOGS_DIR", str(logs))
    monkeypatch.setattr(launcher, "load_env", lambda: {"INTERNAL_SERVICE_TOKEN": "test"})
    monkeypatch.setattr(launcher, "preflight", lambda r, env, platform_path: ["cam"])

    def service_logs() -> str:
        return "\n".join(f"--- {p.name}\n{p.read_text(errors='replace')[-2000:]}"
                         for p in sorted(logs.glob("*.log")))

    try:
        assert launcher.main(["up", "--wait", str(HEALTH_TIMEOUT_S)]) == 0, service_logs()
        for s in launcher.SERVICES:
            assert _health(s.health_port) == 200, service_logs()
        capsys.readouterr()
        assert launcher.main(["status"]) == 0, service_logs()
        out = capsys.readouterr().out
        assert out.count("/health ok") == 3 and "WARN" not in out, out
    finally:
        rc_down = launcher.main(["down", "--timeout", str(EXIT_TIMEOUT_S)])
    out = capsys.readouterr().out
    assert rc_down == 0, service_logs()
    assert out.count("stopped gracefully") == 3 and "force-killed" not in out, out + service_logs()
    for s in launcher.SERVICES:
        assert not (logs / f"{s.name}.pid").exists()
        assert _health(s.health_port) is None, f"{s.name} still answers after down"
    assert "Traceback" not in service_logs(), service_logs()

