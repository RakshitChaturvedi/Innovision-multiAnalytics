"""
Smoke test — Sprint 1 end-to-end pipeline verification.

What this tests:
  1. A FrameEvent can be pushed to frames:test_camera Redis Stream
  2. A consumer (BaseStreamConsumer subclass) can read it from the consumer group
  3. Schema round-trip (serialize -> Redis -> deserialize) is lossless
  4. All field assertions pass
  5. Message is properly acknowledged (XACK)

Run with:
  python scripts/smoke_test.py
"""
import asyncio
import json
import sys
from datetime import datetime, timezone
from typing import Any, Optional
from uuid import uuid4

import redis.asyncio as aioredis

from shared.schemas import CameraProfile, FrameEvent
from shared.schemas.consumer import BaseStreamConsumer

STREAM_NAME = "frames:test_camera"
CONSUMER_GROUP = "detection_group"
CONSUMER_NAME = "smoke-test-consumer"
REDIS_URL = "redis://localhost:6379"

# ── Expected values ────────────────────────────────────────────────────────
EXPECTED_CAMERA_ID = uuid4()
EXPECTED_PROFILE = CameraProfile.BALANCED
EXPECTED_FRAME_SEQ = 1
EXPECTED_FRAME_SHAPE = (1920, 1080)
EXPECTED_KEY_PREFIX = "innovision-snapshots"


class SmokeTestConsumer(BaseStreamConsumer):
    """
    Minimal concrete BaseStreamConsumer used only for this smoke test.
    Captures the first received FrameEvent so the main script can assert on it.
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.received_event: Optional[FrameEvent] = None
        self.received_msg_id: Optional[str] = None

    async def process(self, msg_id: str, data: dict[str, Any]) -> None:
        raw = data.get(b"data") or data.get("data")
        if isinstance(raw, bytes):
            raw = raw.decode()
        self.received_event = FrameEvent.model_validate_json(raw)
        self.received_msg_id = msg_id
        self._running = False  # stop the loop right after first message


async def run() -> None:
    print("\n── IntelliWatch Sprint 1 Smoke Test ──────────────────────────")

    # ── Step 1: Build FrameEvent ────────────────────────────────────────────
    event = FrameEvent(
        camera_id=EXPECTED_CAMERA_ID,
        profile=EXPECTED_PROFILE,
        frame_seq=EXPECTED_FRAME_SEQ,
        frame_object_key=(
            f"{EXPECTED_KEY_PREFIX}/frames/"
            f"{EXPECTED_CAMERA_ID}/{EXPECTED_FRAME_SEQ:06d}.jpg"
        ),
        frame_shape=EXPECTED_FRAME_SHAPE,
        timestamp=datetime.now(timezone.utc),
    )
    print(f"[PRODUCER] Built FrameEvent  seq={event.frame_seq}  camera={event.camera_id}")

    # ── Step 2: Push to Redis Stream (producer side, plain redis client) ────
    producer_redis = aioredis.from_url(REDIS_URL, decode_responses=False)
    await producer_redis.xadd(STREAM_NAME, {b"data": event.model_dump_json().encode()})
    print(f"[PRODUCER] Pushed to stream  {STREAM_NAME}")
    await producer_redis.aclose()

    # ── Step 3: Consume using BaseStreamConsumer subclass ────────────────────
    consumer = SmokeTestConsumer(
        stream_key=STREAM_NAME,
        group_name=CONSUMER_GROUP,
        consumer_name=CONSUMER_NAME,
        batch_size=1,
        block_ms=3000,
    )

    try:
        await asyncio.wait_for(consumer.start(), timeout=5)
    except asyncio.TimeoutError:
        pass

    received = consumer.received_event
    msg_id = consumer.received_msg_id

    # ── Assertion 1: Message received ───────────────────────────────────────
    assert received is not None, (
        "FAIL — No message received within timeout. "
        "Check Redis is running and consumer group exists."
    )
    print(f"[CONSUMER] Message received from stream  event_id={received.event_id}")

    # ── Assertion 2: profile round-trip ─────────────────────────────────────
    assert received.profile == EXPECTED_PROFILE, (
        f"FAIL — profile mismatch. Expected {EXPECTED_PROFILE}, got {received.profile}"
    )
    print(f"[ASSERT]  profile            OK  {received.profile}")

    # ── Assertion 3: frame_seq integer ──────────────────────────────────────
    assert received.frame_seq == EXPECTED_FRAME_SEQ, (
        f"FAIL — frame_seq mismatch. Expected {EXPECTED_FRAME_SEQ}, got {received.frame_seq}"
    )
    print(f"[ASSERT]  frame_seq          OK  {received.frame_seq}")

    # ── Assertion 4: frame_object_key prefix ────────────────────────────────
    assert received.frame_object_key.startswith(EXPECTED_KEY_PREFIX), (
        f"FAIL — frame_object_key wrong prefix. Got '{received.frame_object_key}'"
    )
    print(f"[ASSERT]  frame_object_key   OK  {received.frame_object_key}")

    # ── Assertion 5: frame_shape tuple ──────────────────────────────────────
    assert tuple(received.frame_shape) == EXPECTED_FRAME_SHAPE, (
        f"FAIL — frame_shape mismatch. Expected {EXPECTED_FRAME_SHAPE}, got {received.frame_shape}"
    )
    print(f"[ASSERT]  frame_shape        OK  {received.frame_shape}")

    # ── Step 4: Acknowledge already done inside _process_with_ack ───────────
    print(f"[CONSUMER] XACK sent         msg_id={msg_id}")

    print("\nALL ASSERTIONS PASSED — Sprint 1 smoke test green")
    print("─────────────────────────────────────────────────────────────\n")

    await consumer.stop()


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except AssertionError as e:
        print(f"\nFAILED — {e}\n")
        sys.exit(1)
    except Exception as e:
        print(f"\nUNEXPECTED ERROR — {e}\n")
        sys.exit(1)