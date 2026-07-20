import pytest
from services.ingestion.src.heartbeat import HeartbeatPublisher

@pytest.fixture
def redis_mock(mocker):
    return mocker.patch("services.ingestion.src.heartbeat.aioredis.from_url").return_value

@pytest.fixture
def heartbeat(redis_mock):
    return HeartbeatPublisher()

@pytest.mark.asyncio
async def test_publish_calls_xadd(heartbeat, redis_mock):
    await heartbeat._publish()
    redis_mock.xadd.assert_awaited_once()

@pytest.mark.asyncio
async def test_close_closes_redis(heartbeat, redis_mock):
    await heartbeat.close()
    redis_mock.aclose.assert_awaited_once()