"""
Detects a camera that keeps producing face tracks but no recognition rows.

Every rejection is counted, but a counter nobody watches is how a gate that
rejects 100% of faces (e.g. a blur threshold calibrated for another
resolution) went unnoticed. This turns that state into one WARNING per camera
per outage, naming the reason that rejected the most tracks.

Event time only: the clock is the DetectionEvent timestamp, so replays and
slow consumers behave the same as live traffic.
"""
import logging
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime

logger = logging.getLogger(__name__)


@dataclass
class _CameraWindow:
    since: datetime  # first due face track since the last written row
    reasons: Counter = field(default_factory=Counter)
    warned: bool = False


class StarvationMonitor:
    def __init__(self, starved_after_s: float = 120.0) -> None:
        self.starved_after_s = starved_after_s
        self._windows: dict[str, _CameraWindow] = {}

    def track_due(self, camera_id: str, ts: datetime) -> None:
        """A face track of this camera was due for recognition at event time ts."""
        window = self._windows.get(camera_id)
        if window is None:
            self._windows[camera_id] = _CameraWindow(since=ts)
            return
        if window.warned or (ts - window.since).total_seconds() < self.starved_after_s:
            return
        window.warned = True
        top = window.reasons.most_common(1)
        logger.warning(
            "recognition_starved camera=%s top_reason=%s window_s=%.0f rejections=%s",
            camera_id, top[0][0] if top else "none",
            (ts - window.since).total_seconds(), dict(window.reasons),
        )

    def rejected(self, camera_id: str, reason: str) -> None:
        window = self._windows.get(camera_id)
        if window is not None:
            window.reasons[reason] += 1

    def row_written(self, camera_id: str) -> None:
        window = self._windows.pop(camera_id, None)
        if window is not None and window.warned:
            logger.info("recognition_recovered camera=%s", camera_id)

    def is_starved(self, camera_id: str) -> bool:
        window = self._windows.get(camera_id)
        return window is not None and window.warned
