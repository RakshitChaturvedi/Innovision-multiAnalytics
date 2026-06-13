from unittest.mock import AsyncMock

import pytest

from shared.schemas.consumer import BaseStreamConsumer


class DummyConsumer(BaseStreamConsumer):

    def __init__(self):
        super().__init__(
            stream_key="test_stream",
            group_name="test_group",
            consumer_name="consumer_1",
        )
        self.processed = []

    async def process(self, msg_id, data):
        self.processed.append((msg_id, data))


class FailingConsumer(BaseStreamConsumer):

    def __init__(self):
        super().__init__(
            stream_key="test_stream",
            group_name="test_group",
            consumer_name="consumer_1",
        )

    async def process(self, msg_id, data):
        raise RuntimeError("boom")


@pytest.fixture
def redis_mock():
    return AsyncMock()


@pytest.mark.asyncio
async def test_process_success_acks(redis_mock):
    consumer = DummyConsumer()

    consumer.redis = redis_mock

    await consumer._process_with_ack(
        "1-0",
        {"data": "hello"}
    )

    redis_mock.xack.assert_called_once_with(
        "test_stream",
        "test_group",
        "1-0",
    )


@pytest.mark.asyncio
async def test_process_success_calls_process(redis_mock):
    consumer = DummyConsumer()

    consumer.redis = redis_mock

    await consumer._process_with_ack(
        "1-0",
        {"foo": "bar"}
    )

    assert len(consumer.processed) == 1


@pytest.mark.asyncio
async def test_process_failure_not_acked(redis_mock):
    consumer = FailingConsumer()

    consumer.redis = redis_mock

    await consumer._process_with_ack(
        "1-0",
        {}
    )

    redis_mock.xack.assert_not_called()


@pytest.mark.asyncio
async def test_enter_catch_up_mode(redis_mock):
    consumer = DummyConsumer()

    consumer.redis = redis_mock

    redis_mock.xinfo_groups.return_value = [
        {
            "name": b"test_group",
            "lag": 600,
        }
    ]

    await consumer._check_backpressure()

    assert consumer._catch_up_mode is True


@pytest.mark.asyncio
async def test_exit_catch_up_mode(redis_mock):
    consumer = DummyConsumer()

    consumer.redis = redis_mock

    consumer._catch_up_mode = True

    redis_mock.xinfo_groups.return_value = [
        {
            "name": b"test_group",
            "lag": 50,
        }
    ]

    await consumer._check_backpressure()

    assert consumer._catch_up_mode is False


@pytest.mark.asyncio
async def test_stays_normal_when_lag_low(redis_mock):
    consumer = DummyConsumer()

    consumer.redis = redis_mock

    redis_mock.xinfo_groups.return_value = [
        {
            "name": b"test_group",
            "lag": 20,
        }
    ]

    await consumer._check_backpressure()

    assert consumer._catch_up_mode is False


@pytest.mark.asyncio
async def test_ensure_consumer_group(redis_mock):
    consumer = DummyConsumer()

    consumer.redis = redis_mock

    await consumer._ensure_consumer_group()

    redis_mock.xgroup_create.assert_called_once_with(
        "test_stream",
        "test_group",
        id="0",
        mkstream=True,
    )


@pytest.mark.asyncio
async def test_existing_consumer_group_ignored(redis_mock):
    consumer = DummyConsumer()

    consumer.redis = redis_mock

    redis_mock.xgroup_create.side_effect = Exception(
        "BUSYGROUP"
    )

    await consumer._ensure_consumer_group()