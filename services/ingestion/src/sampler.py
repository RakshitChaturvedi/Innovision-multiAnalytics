from __future__ import annotations
from collections.abc import Callable
import time

class FrameSampler:
    # samples frames to target fps

    def __init__(
            self, 
            target_fps: int, 
            clock: Callable[[], float]=time.monotonic
    ) -> None:
        self._frame_interval = 1.0 / target_fps
        self._last_frame_time = 0.0
        self._clock = clock

    def should_keep_frame(self) -> bool:
        now = self._clock()
        if now - self._last_frame_time >= self._frame_interval:
            self._last_frame_time = now
            return True
        return False
    
    def reset(self) -> None:
        self._last_frame_time = 0.0