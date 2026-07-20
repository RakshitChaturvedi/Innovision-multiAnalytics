from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from shared.frame_cache import FrameCache
from shared.schemas import FrameEvent

from src.camera_monitor import CameraMonitor
from src.config import settings
from src.encoder import JPEGEncoder
from src.ffmpeg_reader import FFmpegReader
from src.heartbeat import HeartbeatPublisher
from src.publisher import FramePublisher
from src.sampler import FrameSampler

class IngestionService:
    def __init__(
            self,
            reader: FFmpegReader,
            sampler: FrameSampler,
            encoder: JPEGEncoder,
            frame_cache: FrameCache,
            publisher: FramePublisher,
            heartbeat: HeartbeatPublisher,
            monitor: CameraMonitor,
    ) -> None:
        self._reader = reader
        self._sampler = sampler
        self._encoder = encoder
        self._frame_cache = frame_cache
        self._publisher = publisher
        self._heartbeat = heartbeat
        self._monitor = monitor

        self._frame_seq = 0
        self._heartbeat_task = None

    async def run(self) -> None:
        self._reader.start()
        self._heartbeat_task = asyncio.create_task(self._heartbeat.run())

        try:
            while True:
                frame = self._reader.read_frame()
                if frame is None:
                    break
                
                self._monitor.frame_recieved()

                if not self._sampler.should_keep_frame():
                    continue

                await self._process_frame(frame)
        finally:
            await self.shutdown()

    async def shutdown(self) -> None:
        if self._heartbeat_task is not None:
            self._heartbeat.stop()
            await self._heartbeat_task
        
        await self._publisher.close()
        await self._heartbeat.close()
        await self._frame_cache.close()

        self._reader.stop()

    async  def _process_frame(self, frame) -> None:
        jpeg = self._encoder.encode(frame)

        self._frame_seq += 1
        frame_reference = self._next_frame_reference()

        await self._frame_cache.put(
            frame_reference=frame_reference,
            frame_bytes=jpeg,
            ttl_seconds=settings.frame_cache_ttl_seconds,
        )
        
        event = FrameEvent(
            camera_id=settings.camera_id,
            frame_reference=frame_reference,
            frame_seq=self._frame_seq,
            frame_timestamp=datetime.now(UTC)
        )

        await self._publisher.publish(event)
    
    def _next_frame_reference(self) -> str:
        return (
            f"{settings.camera_id}:"
            f"{self._frame_seq:08d}"
        )