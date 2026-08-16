"""
Global frame batching.

There is only ONE model: YOLOv11m.

Therefore there is no reason to maintain separate
buckets based on CameraProfile.

All camera frames can enter the same batch.

Example:

    Camera A frame
    Camera B frame
    Camera C frame
    Camera D frame

            ↓

        ONE BATCH

            ↓

        YOLOv11m
"""

import asyncio
import logging
import time

from dataclasses import dataclass, field

import numpy as np

# FIX: shared.schemas.contracts does not exist; the module is events.
from shared.schemas.events import FrameEvent


logger = logging.getLogger(__name__)


# =============================================================
# FRAME ITEM
# =============================================================

@dataclass
class FrameItem:
    """
    A frame waiting for YOLOv11m inference.
    """

    frame_event: FrameEvent

    frame: np.ndarray

    # Redis Stream message id. Carried through the batch so the ACK
    # can be deferred until the frame has actually been published.
    msg_id: str | bytes | None = None

    enqueued_at: float = field(
        default_factory=time.monotonic
    )


# =============================================================
# BATCH READY EVENT
# =============================================================

@dataclass
class BatchReadyEvent:
    """
    A batch ready for YOLOv11m inference.
    """

    items: list[FrameItem]

    @property
    def frames(
        self,
    ) -> list[np.ndarray]:

        return [
            item.frame
            for item in self.items
        ]


# =============================================================
# BATCH MANAGER
# =============================================================

class BatchManager:
    """
    Accumulates frames from ALL cameras.

    Flushes when:

        batch_size is reached

    OR

        oldest frame waits longer than batch_timeout_ms
    """

    def __init__(
        self,
        batch_size: int = 8,
        batch_timeout_ms: int = 100,
        max_pending_batches: int = 4,
    ) -> None:

        self.batch_size = batch_size

        self.batch_timeout_ms = (
            batch_timeout_ms
        )

        # -----------------------------------------------------
        # ONE GLOBAL BUCKET
        # -----------------------------------------------------

        self._items: list[
            FrameItem
        ] = []

        # -----------------------------------------------------
        # READY QUEUE
        # -----------------------------------------------------

        # FIX: the queue was unbounded. If inference fell behind the
        # ingest rate, decoded frames accumulated in RAM until the
        # worker was OOM-killed. A bounded queue pushes back on the
        # Redis consumer instead.
        self._ready_queue: asyncio.Queue[
            BatchReadyEvent
        ] = asyncio.Queue(
            maxsize=max_pending_batches
        )

        # -----------------------------------------------------
        # LOCK
        # -----------------------------------------------------

        self._lock = asyncio.Lock()

        # -----------------------------------------------------
        # BACKGROUND FLUSHER
        # -----------------------------------------------------

        self._flush_task: (
            asyncio.Task | None
        ) = None

    # =========================================================
    # START
    # =========================================================

    async def start(self) -> None:

        self._flush_task = (
            asyncio.create_task(
                self._timeout_flusher()
            )
        )

        logger.info(
            "batch_manager_started "
            "size=%d timeout_ms=%d",
            self.batch_size,
            self.batch_timeout_ms,
        )

    # =========================================================
    # STOP
    # =========================================================

    async def stop(self) -> None:

        if self._flush_task is None:
            return

        self._flush_task.cancel()

        try:
            await self._flush_task

        except asyncio.CancelledError:
            pass

        # -----------------------------------------------------
        # Flush remaining frames
        # -----------------------------------------------------

        async with self._lock:

            await self._flush()

    # =========================================================
    # ADD
    # =========================================================

    async def add(
        self,
        item: FrameItem,
    ) -> None:
        """
        Add a frame to the global batch.
        """

        async with self._lock:

            self._items.append(item)

            if (
                len(self._items)
                >= self.batch_size
            ):
                await self._flush()

    # =========================================================
    # GET READY BATCH
    # =========================================================

    async def get_ready_batch(
        self,
    ) -> BatchReadyEvent:

        return await (
            self._ready_queue.get()
        )

    # =========================================================
    # DEPTH (for metrics / backpressure reporting)
    # =========================================================

    def pending_batches(self) -> int:
        return self._ready_queue.qsize()

    def buffered_frames(self) -> int:
        return len(self._items)

    # =========================================================
    # FLUSH
    # =========================================================

    async def _flush(self) -> None:

        if not self._items:
            return

        items = self._items

        self._items = []

        batch = BatchReadyEvent(
            items=items
        )

        # NOTE: _flush is always called with self._lock held. put()
        # can block when the queue is full, which is intentional
        # backpressure, and safe here because the batch processor
        # never acquires this lock.
        await self._ready_queue.put(
            batch
        )

        logger.debug(
            "batch_flushed size=%d",
            len(items),
        )

    # =========================================================
    # TIMEOUT FLUSHER
    # =========================================================

    async def _timeout_flusher(
        self,
    ) -> None:

        timeout_seconds = (
            self.batch_timeout_ms
            / 1000
        )

        # FIX: a small batch_timeout_ms produced a near-zero sleep and
        # a hot loop that burned a core; clamp it.
        check_interval = max(
            timeout_seconds / 2,
            0.005,
        )

        while True:

            await asyncio.sleep(
                check_interval
            )

            async with self._lock:

                if not self._items:
                    continue

                oldest_age = (
                    time.monotonic()
                    - self._items[
                        0
                    ].enqueued_at
                )

                if (
                    oldest_age
                    >= timeout_seconds
                ):

                    await self._flush()

                    logger.debug(
                        "batch_timeout_flush "
                        "age_ms=%.1f",
                        oldest_age * 1000,
                    )