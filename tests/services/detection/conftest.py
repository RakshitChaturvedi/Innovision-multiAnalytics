import shutil
import socket
import subprocess
import time

import pytest
import redis.asyncio as aioredis


@pytest.fixture(scope="session")
def real_redis_port():
    """A REAL local redis-server (test skipped if the binary is missing)."""
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
async def real_redis(real_redis_port):
    client = aioredis.from_url(f"redis://127.0.0.1:{real_redis_port}")
    await client.flushall()
    yield client
    await client.aclose()
