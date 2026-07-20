import asyncio

from shared.frame_cache import RedisFrameCache

from src.camera_monitor import CameraMonitor
from src.config import settings
from src.encoder import JPEGEncoder
from src.sampler import FrameSampler
from src.publisher import FramePublisher
from src.heartbeat import HeartbeatPublisher
from src.ffmpeg_reader import FFmpegReader
from src.service import IngestionService

async def main() -> None:
    reader = FFmpegReader(
        video_path = settings.video_path,
        width = settings.frame_width,
        height = settings.frame_height
    )
    sampler = FrameSampler(target_fps=settings.target_fps)
    encoder = JPEGEncoder(quality=settings.jpeg_quality)
    frame_cache = RedisFrameCache(redis_url=settings.redis_url)
    publisher = FramePublisher()
    heartbeat = HeartbeatPublisher()
    monitor = CameraMonitor(offline_timeout_seconds=settings.offline_timeout_seconds)
    service = IngestionService(
        reader=reader,
        sampler=sampler,
        encoder=encoder,
        frame_cache=frame_cache,
        publisher=publisher,
        heartbeat=heartbeat,
        monitor=monitor
    )

    try:
        await service.run()
    finally:
        await service.shutdown()

if __name__ == "__main__":
    asyncio.run(main())