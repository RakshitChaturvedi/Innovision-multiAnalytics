"""Install a FAKE `ultralytics` when the real package is missing.

CI/dev boxes without model weights or torch can then import the detection
service (detector/tracker modules import ultralytics at module level) and test
it with a fake YOLO and an IoU-based stand-in for ByteTrack. When the real
package is installed nothing is patched.
"""
import importlib.util
import sys
import types

import numpy as np


class _FakeYOLO:
    def __init__(self, *args, **kwargs):
        pass

    def __call__(self, frames, **kwargs):  # never used: tests inject a detector
        raise RuntimeError("fake YOLO must not run inference")


class _FakeBYTETracker:
    """Greedy IoU tracker: new local ids from 1, dropped after track_buffer misses."""

    def __init__(self, args, frame_rate=30):
        self._buffer = int(getattr(args, "track_buffer", 30))
        self._next = 1
        self._tracks = []  # dicts: id, box, missed

    @staticmethod
    def _iou(a, b):
        ix = max(0.0, min(a[2], b[2]) - max(a[0], b[0]))
        iy = max(0.0, min(a[3], b[3]) - max(a[1], b[1]))
        inter = ix * iy
        union = (a[2] - a[0]) * (a[3] - a[1]) + (b[2] - b[0]) * (b[3] - b[1]) - inter
        return inter / union if union > 0 else 0.0

    def update(self, results, img=None):
        rows = []
        used = set()
        for box, conf, cls in zip(results.xyxy, results.conf, results.cls):
            best, best_iou = None, 0.3
            for i, t in enumerate(self._tracks):
                if i in used:
                    continue
                iou = self._iou(box, t["box"])
                if iou > best_iou:
                    best, best_iou = i, iou
            if best is None:
                self._tracks.append({"id": self._next, "box": box, "missed": 0})
                best = len(self._tracks) - 1
                self._next += 1
            used.add(best)
            self._tracks[best].update(box=box, missed=0)
            rows.append([*box, self._tracks[best]["id"], conf, cls])
        for i, t in enumerate(self._tracks):
            if i not in used:
                t["missed"] += 1
        self._tracks = [t for t in self._tracks if t["missed"] <= self._buffer]
        return np.array(rows, dtype=np.float32) if rows else np.zeros((0, 7), np.float32)


if importlib.util.find_spec("ultralytics") is None:
    ultralytics = types.ModuleType("ultralytics")
    ultralytics.YOLO = _FakeYOLO
    trackers = types.ModuleType("ultralytics.trackers")
    trackers.BYTETracker = _FakeBYTETracker
    ultralytics.trackers = trackers
    sys.modules["ultralytics"] = ultralytics
    sys.modules["ultralytics.trackers"] = trackers
