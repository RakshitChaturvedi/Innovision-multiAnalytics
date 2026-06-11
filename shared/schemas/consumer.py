import logging
import asyncio
import redis.asyncio as aioredis

from typing import Optional, Any
from abc import ABC, abstractmethod

logger = logging.getLogger(__name__)

class BaseStreamConsumer(ABC):
    def __init__(
            self,
            stream_key: str,
            group_name: str,
            consumer_name: str,
            batch_size: int = 50,
            block_ms: int = 100, 
    ):
        self.stream_key = stream_key
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.batch_size = batch_size
        self.block_ms = block_ms

        self.redis: Optional[aioredis.Redis] = None
        self._running = False
        self._catch_up_mode = False
        self.CATCH_UP_THRESHOLD = 500
        self.NORMAL_THRESHOLD = 100

    async def start(self):
        """
            1. connect to redis
            2. create consumer group if not exists
            3. run loop
        """
        from shared.config import settings
        self.redis = await aioredis.from_url(
            f"redis://{settings.REDIS_HOST}:{settings.REDIS_PORT}"
        )
        await self._ensure_consumer_group()

        self._running = True
        logger.info(f"{self.consumer_name} starting on stream {self.stream_key}")
        await self._run_loop()

    async def stop(self):
        self._running = False
        if self.redis:
            await self.redis.aclose()

    async def _ensure_consumer_group(self):
        try:
            await self.redis.xgroup_create(
                self.stream_key, self.group_name, id="0", mkstream=True
            )
        except Exception:
            pass

    async def _run_loop(self):
        """
            messages structure: ->
            [
                (
                    b"stream_name",
                    [
                        (b"message_id", {b"device_id", ...}),  --- Actual payload ---
                    ]
                )
            ]

            1. check backpressure
            2. read message
            3. process message
        """

        while self._running:
            try:
                await self._check_backpressure()
                messages = await self.redis.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.consumer_name,
                    streams={self.stream_key: ">"}, # read new messages only
                    count=self.batch_size,
                    block=self.block_ms
                )

                if not messages:
                    continue
                
                for stream, entries in messages:
                    await asyncio.gather(
                        *[
                            self._process_with_ack(msg_id, data)
                            for msg_id, data in entries
                        ]
                    )
            except Exception as e:
                logger.error(f"{self.consumer_name} loop error: {e}")
                await asyncio.sleep(1)
    
    async def _process_with_ack(self, msg_id: str, data: dict[str, Any]):
        try:
            await self.process(msg_id, data)
            await self.redis.xack(self.stream_key, self.group_name, msg_id)
        except Exception as e:
            logger.error(f"{self.consumer_name} failed on {msg_id}: {e}")

    async def _check_backpressure(self):
        info = await self.redis.xinfo_groups(self.stream_key)

        for group in info:
            if group["name"].decode() == self.group_name:
                lag = group.get("lag", 0)
                if lag > self.CATCH_UP_THRESHOLD and not self._catch_up_mode:
                    logger.warning(f"{self.consumer_name} entering catch-up mode, lag={lag}")
                    self._catch_up_mode = True
                elif lag < self.NORMAL_THRESHOLD and self._catch_up_mode:
                    logger.info(f"{self.consumer_name} exiting catch-up mode")
                    self._catch_up_mode = False

    @abstractmethod
    async def process(self, msg_id: str, data: dict[str, Any]) -> None:
        ...