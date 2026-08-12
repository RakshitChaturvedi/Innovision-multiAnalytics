"""
Controls which frames trigger full recognition inference, and expires
per-track state for tracks that have gone stale.

The detection worker never emits a "track ended" signal, so there's no
event to hook eviction to — state is aged out by a background sweep
instead (see consumer.py's _stale_track_sweeper). Without this, a
long-running stream with many transient people leaks one dict entry per
camera:track_id pair forever.
"""
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)


@dataclass
class TrackSampleState:
    last_sampled_frame_seq: int = 0
    last_quality_score: float = 0.0
    last_seen_monotonic: float = field(default_factory=time.monotonic)


class RecognitionSampler:
    """
    Three triggers to run recognition:
      1. New track       — always recognize on first appearance
      2. Every Nth frame  — periodic confirmation
      3. Quality jump     — a meaningfully better image became available
    """

    def __init__(
        self,
        sample_rate: int = 10,
        quality_improvement_threshold: float = 0.2,
        stale_ttl_seconds: float = 120.0,
    ):
        self.sample_rate = sample_rate
        self.quality_improvement_threshold = quality_improvement_threshold
        self.stale_ttl_seconds = stale_ttl_seconds
        self._states: dict[str, TrackSampleState] = {}

    def should_sample(
        self,
        camera_id: str,
        track_id: int,
        frame_seq: int,
        quality_score: float,
    ) -> bool:
        key = f"{camera_id}:{track_id}"
        now = time.monotonic()

        if key not in self._states:
            self._states[key] = TrackSampleState(
                last_sampled_frame_seq=frame_seq,
                last_quality_score=quality_score,
                last_seen_monotonic=now,
            )
            logger.debug("sampling_new_track camera=%s track=%d", camera_id, track_id)
            return True

        state = self._states[key]
        state.last_seen_monotonic = now

        if frame_seq - state.last_sampled_frame_seq >= self.sample_rate:
            state.last_sampled_frame_seq = frame_seq
            state.last_quality_score = quality_score
            return True

        delta = quality_score - state.last_quality_score
        if delta >= self.quality_improvement_threshold:
            logger.debug("sampling_quality_improvement delta=%.2f track=%d", delta, track_id)
            state.last_sampled_frame_seq = frame_seq
            state.last_quality_score = quality_score
            return True

        return False

    def evict(self, camera_id: str, track_id: int) -> None:
        """Explicit eviction hook, for use if the detection worker ever
        starts emitting a track-ended signal."""
        self._states.pop(f"{camera_id}:{track_id}", None)

    def sweep_stale(self) -> int:
        """
        Drops any track state not touched within stale_ttl_seconds.
        Called periodically by a background task. Returns count evicted.
        """
        now = time.monotonic()
        stale_keys = [
            key for key, state in self._states.items()
            if now - state.last_seen_monotonic > self.stale_ttl_seconds
        ]
        for key in stale_keys:
            del self._states[key]

        if stale_keys:
            logger.info("sampler_swept_stale_tracks count=%d", len(stale_keys))

        return len(stale_keys)

    @property
    def active_tracks(self) -> int:
        return len(self._states)
