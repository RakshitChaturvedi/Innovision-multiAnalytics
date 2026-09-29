"""Reliable Redis Streams consumer.

Every message ends in exactly one of:
  * success              -> XACK
  * PermanentError       -> XADD {stream}:dlq, then XACK (WARNING)
  * any other exception  -> ERROR with traceback, left pending; the reclaim
                            loop (XAUTOCLAIM) redelivers it, and after
                            `max_deliveries` deliveries it goes to the DLQ.

Ordering: messages with the same partition key (default: camera_id) are
processed strictly one after another; different keys run concurrently. A
per-key lock is shared by the main loop and the reclaim loop so the same
camera is never processed by two coroutines at once.
"""

import asyncio
import json
import logging
import random
from abc import ABC, abstractmethod
from typing import Any, Awaitable, Callable, Optional, cast

import redis.asyncio as aioredis
from redis.exceptions import ResponseError

from shared.errors import PermanentError

logger = logging.getLogger(__name__)

Item = tuple[str, str, dict]  # (stream, msg_id, data); empty payloads are acked as lost before this


def _text(v: Any) -> str:
    return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else str(v)


class BaseStreamConsumer(ABC):
    # Subclasses that ack after their own batching (detection) set this to
    # False and call `await self.ack(stream, msg_id)` when the work is done.
    auto_ack: bool = True
    # Where a newly created consumer group starts reading.
    group_start_id: str = "0"

    def __init__(
        self,
        streams: list[str],
        group_name: str,
        consumer_name: str,
        batch_size: int = 50,
        block_ms: int = 1000,
        max_deliveries: int = 5,
        reclaim_idle_ms: int = 30000,
        reclaim_interval_s: float = 15,
        stats_interval_s: float = 60,
        stop_timeout_s: float = 10,
    ):
        self.streams: list[str] = list(streams)
        self.group_name = group_name
        self.consumer_name = consumer_name
        self.batch_size = batch_size
        self.block_ms = block_ms
        self.max_deliveries = max_deliveries
        self.reclaim_idle_ms = reclaim_idle_ms
        self.reclaim_interval_s = reclaim_interval_s
        self.stats_interval_s = stats_interval_s
        self.stop_timeout_s = stop_timeout_s

        self.redis: Optional[aioredis.Redis] = None
        self._running = False
        self._started = False
        self._loop_done = asyncio.Event()
        # Set first thing in stop(): background tasks that die from here on are
        # shutting down, not crashing (no ERROR log, no restart).
        self._shutdown = asyncio.Event()
        self._inflight: set[asyncio.Task] = set()
        self._active: set[tuple[str, str]] = set()  # (stream, msg_id) queued/processing
        self._key_locks: dict[str, asyncio.Lock] = {}
        self._key_refs: dict[str, int] = {}
        self._bg_tasks: list[asyncio.Task] = []
        self._counters = {
            "processed": 0,
            "failed": 0,
            "dlq": 0,
            "reclaimed": 0,
            "lost": 0,  # pending entries whose payload was trimmed from the stream
        }

    # ------------------------------------------------------------------
    # public API
    # ------------------------------------------------------------------

    def stats(self) -> dict[str, Any]:
        return {
            **self._counters,
            "consumer": self.consumer_name,
            "group": self.group_name,
            "streams": list(self.streams),
            "inflight": len(self._inflight),
            "running": self._running,
        }

    async def start(self) -> None:
        """connect -> create groups -> drain own pending -> reclaim task -> loop"""
        from shared.config import settings

        self.redis = aioredis.from_url(settings.redis_url(), health_check_interval=30)
        await self._ensure_groups(self.streams)

        self._shutdown.clear()
        self._running = True
        self._started = True
        self._loop_done.clear()
        logger.info(
            "%s starting group=%s streams=%s", self.consumer_name, self.group_name, self.streams
        )
        try:
            await self._drain_own_pending()
            self._bg_tasks = [
                asyncio.create_task(
                    self._supervise("reclaim", self._reclaim_loop),
                    name=f"{self.consumer_name}-reclaim",
                ),
                asyncio.create_task(
                    self._supervise("stats", self._stats_loop),
                    name=f"{self.consumer_name}-stats",
                ),
            ]
            await self._run_loop()
        finally:
            self._loop_done.set()

    async def stop(self) -> None:
        """Stop reading, let in-flight work finish (timeout), close Redis."""
        self._shutdown.set()
        self._running = False

        if self._started:
            try:
                await asyncio.wait_for(self._loop_done.wait(), self.stop_timeout_s)
            except asyncio.TimeoutError:
                logger.warning("%s main loop did not exit in time", self.consumer_name)

        inflight = [t for t in self._inflight if not t.done()]
        if inflight:
            _, pending = await asyncio.wait(inflight, timeout=self.stop_timeout_s)
            if pending:
                logger.warning(
                    "%s abandoning %d in-flight task(s) after %.0fs; their messages stay "
                    "pending and will be reclaimed",
                    self.consumer_name, len(pending), self.stop_timeout_s,
                )
                for t in pending:
                    t.cancel()
                await asyncio.gather(*pending, return_exceptions=True)

        for t in self._bg_tasks:
            t.cancel()
        await asyncio.gather(*self._bg_tasks, return_exceptions=True)
        self._bg_tasks = []

        if self.redis is not None:
            await self.redis.aclose()
            self.redis = None

    async def add_stream(self, stream: str) -> None:
        if stream in self.streams:
            return
        if self.redis is not None:
            await self._ensure_group(stream)
        self.streams.append(stream)
        logger.info("%s added stream %s", self.consumer_name, stream)

    async def remove_stream(self, stream: str) -> None:
        # Entries already pending for this stream stay in Redis untouched.
        if stream in self.streams:
            self.streams.remove(stream)
            logger.info("%s removed stream %s", self.consumer_name, stream)

    async def ack(self, stream: str, msg_id: str | bytes) -> None:
        assert self.redis is not None
        await self._r.xack(stream, self.group_name, msg_id)
        self._counters["processed"] += 1

    @property
    def _r(self) -> aioredis.Redis:
        """self.redis for typing only; still None before start(), as before."""
        return cast(aioredis.Redis, self.redis)

    def partition_key(self, stream: str, data: dict) -> str:
        """Messages with the same key are processed strictly in order."""
        raw = data.get(b"data") or data.get("data")
        if raw is None:
            return stream
        try:
            camera_id = json.loads(raw).get("camera_id")
        except (ValueError, AttributeError, TypeError):
            return stream
        return str(camera_id) if camera_id else stream

    @abstractmethod
    async def process(self, msg_id: str, data: dict[str, Any], stream: str) -> None:
        ...

    # ------------------------------------------------------------------
    # groups
    # ------------------------------------------------------------------

    async def _ensure_group(self, stream: str) -> None:
        assert self.redis is not None
        try:
            await self.redis.xgroup_create(
                stream, self.group_name, id=self.group_start_id, mkstream=True
            )
        except ResponseError as exc:
            if "BUSYGROUP" not in str(exc):
                raise

    async def _ensure_groups(self, streams: list[str]) -> None:
        for stream in list(streams):
            await self._ensure_group(stream)

    # ------------------------------------------------------------------
    # main loop
    # ------------------------------------------------------------------

    async def _run_loop(self) -> None:
        backoff = 0.5
        while self._running:
            streams = list(self.streams)
            if not streams:
                await asyncio.sleep(self.block_ms / 1000)
                continue
            try:
                result: Any = await self._r.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.consumer_name,
                    streams={s: ">" for s in streams},
                    count=self.batch_size,
                    block=self.block_ms,
                )
                backoff = 0.5
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self._on_redis_error("xreadgroup", exc)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5)
                continue

            if result:
                await self._dispatch(await self._entries_to_items(result))

    async def _on_redis_error(self, where: str, exc: Exception) -> None:
        logger.error("%s redis error in %s: %r", self.consumer_name, where, exc)
        if "NOGROUP" in str(exc):
            # Redis lost the group (restart without persistence, flush, ...)
            try:
                await self._ensure_groups(self.streams)
                logger.warning("%s recreated consumer groups after NOGROUP", self.consumer_name)
            except Exception as gexc:
                logger.error("%s could not recreate groups: %r", self.consumer_name, gexc)

    async def _entries_to_items(self, result) -> list[Item]:
        items: list[Item] = []
        for stream, entries in result:
            stream = _text(stream)
            for msg_id, data in entries:
                msg_id = _text(msg_id)
                if not data:
                    await self._ack_lost(stream, msg_id)
                    continue
                items.append((stream, msg_id, data))
        return items

    async def _ack_lost(self, stream: str, msg_id: str) -> None:
        """A pending entry whose payload was trimmed out of the stream."""
        self._counters["lost"] += 1
        logger.warning(
            "%s message %s on %s was trimmed from the stream before it was processed; "
            "acking (data lost)", self.consumer_name, msg_id, stream,
        )
        try:
            await self._r.xack(stream, self.group_name, msg_id)
        except Exception:
            logger.exception("%s could not ack lost message %s", self.consumer_name, msg_id)

    # ------------------------------------------------------------------
    # partitioned dispatch
    # ------------------------------------------------------------------

    async def _dispatch(self, items: list[Item]) -> None:
        """Group by partition key: sequential within a key, concurrent across keys.

        Uses asyncio.wait on tasks (not gather on coroutines) so that if the
        caller is cancelled during shutdown the in-flight messages keep
        running and stop() can wait for them.
        """
        by_key: dict[str, list[Item]] = {}
        for item in items:
            stream, msg_id, data = item
            try:
                key = self.partition_key(stream, data)
            except Exception:
                logger.exception("partition_key failed for %s; using stream name", msg_id)
                key = stream
            self._active.add((stream, msg_id))
            by_key.setdefault(key, []).append(item)

        tasks = []
        for key, key_items in by_key.items():
            self._key_refs[key] = self._key_refs.get(key, 0) + 1
            task = asyncio.create_task(self._run_key(key, key_items))
            self._inflight.add(task)
            task.add_done_callback(self._inflight.discard)
            tasks.append(task)
        if tasks:
            await asyncio.wait(tasks)

    async def _run_key(self, key: str, items: list[Item]) -> None:
        lock = self._key_locks.setdefault(key, asyncio.Lock())
        try:
            async with lock:
                for stream, msg_id, data in items:
                    try:
                        await self._handle(stream, msg_id, data)
                    finally:
                        self._active.discard((stream, msg_id))
        finally:
            for stream, msg_id, _ in items:
                self._active.discard((stream, msg_id))
            self._key_refs[key] -= 1
            if self._key_refs[key] <= 0:
                self._key_refs.pop(key, None)
                self._key_locks.pop(key, None)

    async def _handle(self, stream: str, msg_id: str, data: dict) -> None:
        permanent: Optional[PermanentError] = None
        try:
            await self.process(msg_id, data, stream)
        except PermanentError as exc:
            permanent = exc
        except asyncio.CancelledError:
            raise
        except Exception:
            self._counters["failed"] += 1
            logger.exception(
                "%s failed on %s %s; left pending for redelivery",
                self.consumer_name, stream, msg_id,
            )
            return

        try:
            if permanent is not None:
                await self._dead_letter(stream, msg_id, data, f"{type(permanent).__name__}: {permanent}")
            elif self.auto_ack:
                await self.ack(stream, msg_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            self._counters["failed"] += 1
            logger.exception(
                "%s could not ack/dead-letter %s %s; left pending",
                self.consumer_name, stream, msg_id,
            )

    # ------------------------------------------------------------------
    # DLQ
    # ------------------------------------------------------------------

    @staticmethod
    def _payload_text(data: Optional[dict]) -> str:
        if not data:
            return ""
        raw = data.get(b"data")
        if raw is None:
            raw = data.get("data")
        if raw is not None and len(data) == 1:
            return _text(raw)
        return json.dumps({_text(k): _text(v) for k, v in data.items()})

    async def _dead_letter(self, stream: str, msg_id: str, data: Optional[dict], error: str) -> None:
        # XADD first, then XACK: a crash in between duplicates a DLQ entry
        # but never loses the message.
        await self._r.xadd(
            f"{stream}:dlq",
            {
                "data": self._payload_text(data),
                "error": error,
                "group": self.group_name,
                "msg_id": msg_id,
            },
        )
        await self._r.xack(stream, self.group_name, msg_id)
        self._counters["dlq"] += 1
        logger.warning(
            "%s dead-lettered %s %s to %s:dlq error=%s",
            self.consumer_name, stream, msg_id, stream, error,
        )

    # ------------------------------------------------------------------
    # startup: own pending entries
    # ------------------------------------------------------------------

    async def _drain_own_pending(self) -> None:
        """Process what this consumer name left pending before a crash.

        Reading with a concrete id returns this consumer's history; a cursor
        advances past entries that fail again so we never spin on them (the
        reclaim loop owns retries and the delivery limit).
        """
        for stream in list(self.streams):
            cursor = "0"
            while self._running:
                result: Any = await self._r.xreadgroup(
                    groupname=self.group_name,
                    consumername=self.consumer_name,
                    streams={stream: cursor},
                    count=self.batch_size,
                )
                entries = result[0][1] if result else []
                if not entries:
                    break
                cursor = _text(entries[-1][0])
                items = await self._entries_to_items([(stream, entries)])
                if items:
                    logger.info(
                        "%s draining %d own pending message(s) on %s",
                        self.consumer_name, len(items), stream,
                    )
                    await self._dispatch(items)

    # ------------------------------------------------------------------
    # reclaim
    # ------------------------------------------------------------------

    async def _reclaim_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.reclaim_interval_s)
            await self._reclaim_once()

    async def _reclaim_once(self) -> None:
        for stream in list(self.streams):
            cursor = "0-0"
            while self._running:
                res: Any = await self._r.xautoclaim(
                    stream,
                    self.group_name,
                    self.consumer_name,
                    min_idle_time=self.reclaim_idle_ms,
                    start_id=cursor,
                    count=self.batch_size,
                )
                cursor = _text(res[0])
                entries = res[1]
                deleted = res[2] if len(res) > 2 else []  # Redis >= 7

                for msg_id in deleted:
                    await self._ack_lost(stream, _text(msg_id))

                if entries:
                    await self._process_reclaimed(stream, entries)

                if cursor == "0-0":
                    break

    async def _process_reclaimed(self, stream: str, entries: list) -> None:
        ids = [_text(e[0]) for e in entries]
        counts = await self._delivery_counts(stream, ids)

        items: list[Item] = []
        for (raw_id, data), msg_id in zip(entries, ids):
            if not data:
                await self._ack_lost(stream, msg_id)
                continue
            if (stream, msg_id) in self._active:
                continue  # still being processed by us; not a stuck message
            self._counters["reclaimed"] += 1
            delivered = counts.get(msg_id, 0)
            if delivered > self.max_deliveries:
                try:
                    await self._dead_letter(
                        stream, msg_id, data,
                        f"max_deliveries exceeded: delivered {delivered} times "
                        f"(limit {self.max_deliveries})",
                    )
                except asyncio.CancelledError:
                    raise
                except Exception:
                    self._counters["failed"] += 1
                    logger.exception("%s could not dead-letter %s; left pending", self.consumer_name, msg_id)
                continue
            logger.warning(
                "%s reclaimed %s %s (delivery %d/%d)",
                self.consumer_name, stream, msg_id, delivered, self.max_deliveries,
            )
            items.append((stream, msg_id, data))

        if items:
            await self._dispatch(items)

    async def _delivery_counts(self, stream: str, ids: list[str]) -> dict[str, int]:
        """times_delivered per id (XAUTOCLAIM has already counted this claim)."""
        async with self._r.pipeline(transaction=False) as pipe:
            for msg_id in ids:
                pipe.xpending_range(stream, self.group_name, min=msg_id, max=msg_id, count=1)
            results = await pipe.execute()
        counts: dict[str, int] = {}
        for msg_id, rows in zip(ids, results):
            if rows:
                counts[msg_id] = int(rows[0]["times_delivered"])
        return counts

    # ------------------------------------------------------------------
    # background tasks
    # ------------------------------------------------------------------

    async def _supervise(self, name: str, factory: Callable[[], Awaitable[None]]) -> None:
        """Run a background loop; if it dies, log and restart it. Never silent."""
        while self._running:
            try:
                await factory()
                return
            except asyncio.CancelledError:
                raise
            except Exception:
                if self._shutdown.is_set():
                    logger.debug(
                        "%s background task %s stopped during shutdown",
                        self.consumer_name, name, exc_info=True,
                    )
                    return
                logger.exception(
                    "%s background task %s crashed; restarting", self.consumer_name, name
                )
                try:
                    await asyncio.wait_for(self._shutdown.wait(), 1 + random.random())
                except TimeoutError:
                    pass

    async def _stats_loop(self) -> None:
        while self._running:
            await asyncio.sleep(self.stats_interval_s)
            logger.info("%s stats %s", self.consumer_name, self.stats())
