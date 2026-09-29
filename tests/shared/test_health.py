"""GET /health. REAL redis-server (fixture) and REAL Postgres at
TEST_DATABASE_URL's server (skips if unreachable). Consumers are fakes."""
import asyncio
import json
import os
import signal
import socket
import time

import pytest
from sqlalchemy import make_url

from shared.health import HealthServer, db_check, health_port, redis_check
from shared.runner import run_consumers


def _pg_url() -> str:
    url = os.environ.get(
        "TEST_DATABASE_URL",
        "postgresql+asyncpg://postgres:postgres@127.0.0.1:5432/innovision_analytics_test",
    )
    return make_url(url).set(database="postgres").render_as_string(hide_password=False)


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def get(port: int, path: str = "/health") -> tuple[int, dict]:
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(f"GET {path} HTTP/1.1\r\nHost: x\r\n\r\n".encode())
    await writer.drain()
    raw = await asyncio.wait_for(reader.read(), 10)
    writer.close()
    head, _, body = raw.partition(b"\r\n\r\n")
    return int(head.split()[1]), json.loads(body)


class FakeConsumer:
    def __init__(self, name="c1", last=None):
        self.name, self.last_processed_monotonic = name, last

    def stats(self):
        return {"consumer": self.name, "processed": 3, "failed": 0, "dlq": 0}


@pytest.fixture
async def checks(redis_port):
    r_check, r_close = redis_check(f"redis://127.0.0.1:{redis_port}")
    d_check, d_close = db_check(_pg_url())
    try:
        await asyncio.wait_for(d_check(), 5)
    except Exception as exc:
        await r_close(); await d_close()
        pytest.skip(f"no Postgres: {exc}")
    yield r_check, d_check
    await r_close()
    await d_close()


async def serve(consumers, tasks, r_check, d_check):
    srv = HealthServer("svc", consumers, tasks, port=0, host="127.0.0.1",
                       redis_check=r_check, db_check=d_check)
    await srv.start()
    return srv


async def forever():
    await asyncio.sleep(3600)


async def test_healthy_returns_200_with_stats(checks):
    task = asyncio.create_task(forever())
    srv = await serve([FakeConsumer(last=time.monotonic() - 2), FakeConsumer("idle")],
                      [task], *checks)
    try:
        status, body = await get(srv.port)
    finally:
        await srv.stop(); task.cancel()
    assert status == 200
    assert body["status"] == "ok"
    assert body["redis"] == {"ok": True} and body["db"] == {"ok": True}
    assert body["consumers_alive"] is True
    assert 2 <= body["seconds_since_last_message"] < 10
    c1, idle = body["consumers"]
    assert c1["processed"] == 3 and c1["consumer"] == "c1"
    assert idle["seconds_since_last_message"] is None  # idle is not unhealthy


async def test_redis_down_returns_503(checks):
    _, d_check = checks
    r_check, r_close = redis_check(f"redis://127.0.0.1:{_free_port()}")
    task = asyncio.create_task(forever())
    srv = await serve([FakeConsumer()], [task], r_check, d_check)
    try:
        status, body = await get(srv.port)
    finally:
        await srv.stop(); task.cancel(); await r_close()
    assert status == 503
    assert body["redis"]["ok"] is False and "error" in body["redis"]
    assert body["db"]["ok"] is True


async def test_db_down_returns_503(checks):
    r_check, _ = checks
    d_check, d_close = db_check(
        f"postgresql+asyncpg://x:y@127.0.0.1:{_free_port()}/nope"
    )
    task = asyncio.create_task(forever())
    srv = await serve([FakeConsumer()], [task], r_check, d_check)
    try:
        status, body = await get(srv.port)
    finally:
        await srv.stop(); task.cancel(); await d_close()
    assert status == 503
    assert body["db"]["ok"] is False and body["redis"]["ok"] is True


async def test_dead_consumer_task_returns_503(checks):
    async def crash():
        raise RuntimeError("boom")

    task = asyncio.create_task(crash(), name="consumer-0")
    await asyncio.sleep(0)
    srv = await serve([FakeConsumer()], [task], *checks)
    try:
        status, body = await get(srv.port)
    finally:
        await srv.stop()
    assert status == 503
    assert body["consumers_alive"] is False
    assert "boom" in body["dead_tasks"][0]


async def test_hung_dependency_times_out_as_503(checks, monkeypatch):
    import shared.health as health

    monkeypatch.setattr(health, "PING_TIMEOUT_S", 0.2)

    async def hang():
        await asyncio.sleep(60)

    task = asyncio.create_task(forever())
    srv = await serve([FakeConsumer()], [task], checks[0], hang)
    try:
        status, body = await asyncio.wait_for(get(srv.port), 5)
    finally:
        await srv.stop(); task.cancel()
    assert status == 503 and "TimeoutError" in body["db"]["error"]


async def test_other_paths_are_404(checks):
    srv = await serve([], [], *checks)
    try:
        status, _ = await get(srv.port, "/metrics")
    finally:
        await srv.stop()
    assert status == 404


def test_health_port_default_and_env(monkeypatch):
    monkeypatch.delenv("HEALTH_PORT", raising=False)
    assert health_port(8083) == 8083
    monkeypatch.setenv("HEALTH_PORT", "9999")
    assert health_port(8083) == 9999


class RunnerConsumer(FakeConsumer):
    def __init__(self):
        super().__init__()
        self.stopped = False

    async def start(self):
        await asyncio.sleep(3600)

    async def stop(self):
        self.stopped = True


async def test_run_consumers_serves_health_and_runs_background_jobs(redis_port, checks):
    port = _free_port()
    ran = asyncio.Event()

    async def job():
        ran.set()
        await asyncio.sleep(3600)

    c = RunnerConsumer()
    task = asyncio.create_task(run_consumers(
        "t", [c], health_port=port, background={"job": job},
        redis_url=f"redis://127.0.0.1:{redis_port}", database_url=_pg_url(),
    ))
    for _ in range(50):
        try:
            status, body = await get(port)
            break
        except OSError:
            await asyncio.sleep(0.05)
    await asyncio.wait_for(ran.wait(), 5)
    os.kill(os.getpid(), signal.SIGTERM)
    await asyncio.wait_for(task, 10)
    assert status == 200 and body["service"] == "t"
    assert c.stopped
    with pytest.raises(OSError):  # server closed on shutdown
        await asyncio.open_connection("127.0.0.1", port)


async def test_last_processed_is_set_on_ack(redis):
    from shared.schemas.consumer import BaseStreamConsumer

    class C(BaseStreamConsumer):
        async def process(self, msg_id, data, stream):
            pass

    c = C(["s"], "g", "c")
    c.redis = redis
    await redis.xgroup_create("s", "g", id="0", mkstream=True)
    msg = await redis.xadd("s", {"data": "{}"})
    assert c.last_processed_monotonic is None
    await c.ack("s", msg)
    assert time.monotonic() - c.last_processed_monotonic < 1
