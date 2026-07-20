import pytest
from services.ingestion.src.publisher import FramePublisher

@pytest.fixture
def redis_mock(mocker):
    return mocker.patch("services.ingestion.src.publisher.aioredis.from_url").return_value

@pytest.fixture
def publisher(redis_mock):
    return FramePublisher()

@pytest.fixture
def frame_event():
    class DummyFrameEvent:
        def model_dump(self, mode="json"):
            return {
                "camera_id": "cam01",
                "frame_reference": "cam01:000001",
                "frame_seq": 1,
            }
    return DummyFrameEvent()

@pytest.mark.asyncio
async def test_publish_calls_xadd(
    publisher,
    frame_event,
    redis_mock,
):
    await publisher.publish(frame_event)
    redis_mock.xadd.assert_called_once()


@pytest.mark.asyncio
async def test_close_closes_redis(publisher,redis_mock):
    await publisher.close()
    redis_mock.aclose.assert_awaited_once()