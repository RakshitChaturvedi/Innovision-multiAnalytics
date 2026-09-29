"""Retention job: deletes old raw/event rows in batches.

Runs hourly under shared/supervisor.supervise (see event_processing main).
The cutoff uses the wall clock: this is a sweeper over stored history, not
business logic on events (CLAUDE.md rule 3).

The set of tables is FIXED below. intruder_events, headcount_breach_events
and enrollment face_embeddings are never touched.
"""
import asyncio
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from pydantic import Field
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from shared.config import settings as shared_settings
from shared.service_settings import ServiceSettings

logger = logging.getLogger(__name__)


class RetentionConfig(ServiceSettings):
    DATABASE_URL: str = Field(default_factory=lambda: shared_settings.DATABASE_URL)
    RETENTION_DAYS_RAW: float = 7
    RETENTION_DAYS_EVENTS: float = 30
    RETENTION_INTERVAL_S: float = 3600
    RETENTION_BATCH_SIZE: int = 10_000


@dataclass(frozen=True)
class Rule:
    table: str
    ts_column: str
    kind: str  # "raw" | "events"
    extra_where: str = ""


# Order matters: recognition_events before face_embeddings, so the embedding
# delete (ON DELETE CASCADE into recognition_events) never removes a
# recognition row that is still inside its retention window.
RULES: tuple[Rule, ...] = (
    Rule("detection_events", "frame_timestamp", "raw"),
    Rule("recognition_events", "timestamp", "raw"),
    Rule(
        "face_embeddings", "created_at", "raw",
        "AND is_enrollment = false "
        "AND NOT EXISTS (SELECT 1 FROM recognition_events r WHERE r.embedding_id = t.id)",
    ),
    Rule("zone_events", "timestamp", "events"),
    Rule("headcount_snapshots", "timestamp", "events"),
)


class RetentionJob:
    def __init__(self, config: RetentionConfig | None = None, engine: AsyncEngine | None = None):
        self.config = config or RetentionConfig()
        self._own_engine = engine is None
        self._engine = engine or create_async_engine(self.config.DATABASE_URL, pool_size=1,
                                                     max_overflow=0, pool_pre_ping=True)

    async def close(self) -> None:
        if self._own_engine:
            await self._engine.dispose()

    def _cutoff(self, kind: str, now: datetime) -> datetime:
        days = self.config.RETENTION_DAYS_RAW if kind == "raw" else self.config.RETENTION_DAYS_EVENTS
        return now - timedelta(days=days)

    async def _delete_table(self, rule: Rule, cutoff: datetime) -> int:
        stmt = text(
            f"DELETE FROM {rule.table} WHERE ctid IN ("
            f" SELECT t.ctid FROM {rule.table} t"
            f" WHERE t.{rule.ts_column} < :cutoff {rule.extra_where}"
            f" LIMIT :batch)"
        )
        total = 0
        while True:
            async with self._engine.begin() as conn:  # one transaction per batch
                result = await conn.execute(
                    stmt, {"cutoff": cutoff, "batch": self.config.RETENTION_BATCH_SIZE}
                )
            n = result.rowcount or 0
            total += n
            if n < self.config.RETENTION_BATCH_SIZE:
                return total
            await asyncio.sleep(0)  # let the consumers breathe between batches

    async def run_once(self, now: datetime | None = None) -> dict[str, int]:
        now = now or datetime.now(timezone.utc)
        deleted: dict[str, int] = {}
        for rule in RULES:
            deleted[rule.table] = await self._delete_table(rule, self._cutoff(rule.kind, now))
        logger.info(
            "retention_run deleted_total=%d %s",
            sum(deleted.values()),
            " ".join(f"{t}={n}" for t, n in deleted.items()),
        )
        return deleted

    async def run_forever(self) -> None:
        """Supervised loop. A failing run raises: supervise() logs and restarts it."""
        while True:
            await self.run_once()
            await asyncio.sleep(self.config.RETENTION_INTERVAL_S)
