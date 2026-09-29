import asyncio
import logging

import redis.asyncio as aioredis
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from shared.schemas.consumer import BaseStreamConsumer
from shared.schemas.events import DetectionEvent

from .config import config
from .event_writer import ZoneEventWriter
from .processor import ZoneProcessor
from .supervisor import supervise
from .track_state_store import ActiveTrackStore
from .zone_store import ZoneStore


logger = logging.getLogger(__name__)


class ZoneMonitorConsumer(BaseStreamConsumer):

    def __init__(self):

        super().__init__(
            streams=[config.DETECTIONS_STREAM],
            group_name=config.CONSUMER_GROUP,
            consumer_name=config.CONSUMER_NAME,
        )

        self._engine = create_async_engine(
            config.DATABASE_URL
        )

        self._session_factory = async_sessionmaker(
            self._engine,
            expire_on_commit=False,
        )

        self._zone_store: ZoneStore | None = None
        self._processor: ZoneProcessor | None = None
        self._sweeper_task: asyncio.Task | None = None

    async def start(self):

        redis_client = await aioredis.from_url(
            f"redis://{config.REDIS_HOST}:{config.REDIS_PORT}"
        )

        self._zone_store = ZoneStore(
            session_factory=self._session_factory,
            stopping=self._shutdown,
        )

        await self._zone_store.initialize(
            redis_client
        )

        self._processor = ZoneProcessor(
            self._zone_store,
            ActiveTrackStore(redis_client),
            ZoneEventWriter(self._session_factory, redis_client),
        )

        self._sweeper_task = asyncio.create_task(
            supervise(
                "zone_stale_camera_sweeper",
                self._processor.run_sweeper,
                stopping=self._shutdown,
            )
        )

        logger.info(
            "zone_monitor_consumer_ready"
        )

        await super().start()

    async def stop(self):
        self._shutdown.set()  # background tasks dying from here on are stopping
        for task in (self._sweeper_task,):
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        if self._zone_store:
            await self._zone_store.close()
        await super().stop()

    async def process(
        self,
        msg_id: str,
        data: dict,
        stream: str,
    ) -> None:

        raw = (
            data.get(b"data")
            or data.get("data")
        )

        if isinstance(raw, bytes):
            raw = raw.decode()

        detection_event = (
            DetectionEvent.model_validate_json(raw)
        )

        await self._processor.handle(detection_event)
