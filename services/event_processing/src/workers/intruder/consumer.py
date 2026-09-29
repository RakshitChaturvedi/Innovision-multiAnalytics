import asyncio
import logging

import redis.asyncio as aioredis
from pydantic import ValidationError
from sqlalchemy.ext.asyncio import (
    async_sessionmaker,
    create_async_engine,
)

from shared.alerting.publisher import AlertPublisher
from shared.errors import PermanentError
from shared.schemas.consumer import BaseStreamConsumer
from shared.schemas.events import ZoneEvent
from shared.supervisor import supervise

from .config import config
from .processor import IntruderProcessor
from .repository import IntruderRepository


logger = logging.getLogger(__name__)


class IntruderConsumer(BaseStreamConsumer):

    def __init__(self):

        super().__init__(
            streams=[config.ZONE_EVENTS_STREAM],
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

        self._publisher: aioredis.Redis | None = None
        self._processor: IntruderProcessor | None = None
        self._sweeper: asyncio.Task | None = None

    async def start(self):

        self._publisher = await aioredis.from_url(
            f"redis://{config.REDIS_HOST}:{config.REDIS_PORT}/{config.REDIS_DB}"
        )

        self._processor = IntruderProcessor(
            IntruderRepository(self._session_factory),
            AlertPublisher(
                self._publisher,
                stream=config.ALERTS_STREAM,
                maxlen=config.ALERTS_MAXLEN,
            ),
        )

        self._sweeper = asyncio.create_task(
            supervise(
                "intruder_candidate_sweeper",
                self._sweep_forever,
                stopping=self._shutdown,
            )
        )

        logger.info(
            "intruder_consumer_ready"
        )

        await super().start()

    async def _sweep_forever(self):
        """Wall-clock loop over grace-period candidates; crashes propagate
        to the supervisor, which logs and restarts it."""
        while not self._shutdown.is_set():
            await self._processor.sweep()
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(), config.INTRUDER_SWEEP_INTERVAL_S
                )
            except TimeoutError:
                pass

    async def stop(self):

        self._shutdown.set()
        if self._sweeper is not None:
            self._sweeper.cancel()
            await asyncio.gather(self._sweeper, return_exceptions=True)
            self._sweeper = None

        await super().stop()

        if self._publisher is not None:
            await self._publisher.aclose()

        await self._engine.dispose()

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

        if raw is None:
            raise PermanentError(
                f"zone event {msg_id} has no 'data' field"
            )

        if isinstance(raw, bytes):
            raw = raw.decode()

        try:
            zone_event = ZoneEvent.model_validate_json(raw)
        except ValidationError as exc:
            raise PermanentError(
                f"zone event {msg_id} is not a valid ZoneEvent: {exc}"
            ) from exc

        await self._processor.handle(zone_event)
