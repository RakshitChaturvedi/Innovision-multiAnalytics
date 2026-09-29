from collections import deque


class RollingWindow:
    """In-memory rolling window of (event_ts, count) for one zone."""

    def __init__(self, seconds: float):
        self._seconds = seconds
        self._samples: deque[tuple[float, int]] = deque()
        self.dropped = 0

    def add(self, ts: float, count: int) -> float:
        """Add a sample and return the rolling average.

        A sample not newer than the latest one (redelivery or out of order)
        is ignored, so retries are idempotent; the current average is returned.
        """
        if self._samples and ts <= self._samples[-1][0]:
            self.dropped += 1
            return self.average()
        self._samples.append((ts, count))
        cutoff = ts - self._seconds
        while len(self._samples) > 1 and self._samples[0][0] < cutoff:
            self._samples.popleft()
        return self.average()

    def average(self) -> float:
        if not self._samples:
            return 0.0
        return sum(c for _, c in self._samples) / len(self._samples)
