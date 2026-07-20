from __future__ import annotations
import time

class CameraMonitor:
    def __init__(self, offline_timeout_seconds: int) -> None:
        self._offline_timeout = offline_timeout_seconds
        self._last_frame_time = time.monotonic()

    def frame_received(self) -> None:
        self._last_frame_time = time.monotonic()

    def is_online(self) -> bool:
        elapsed = time.monotonic() - self._last_frame_time
        return elapsed < self._offline_timeout
    
    def is_offline(self) -> bool:
        return not self.is_online()
    
    def seconds_since_last_frame(self) -> float:
        return time.monotonic() - self._last_frame_time