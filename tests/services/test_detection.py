import asyncio
import json
import unittest
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import numpy as np

from shared.schemas.common import BoundingBox, TrackResult
from shared.schemas.enums import CameraProfile, FrameProvider
from shared.schemas.events import DetectionEvent, FrameEvent
from services.detection.src.batching import BatchManager, FrameItem
from services.detection.src.config import settings
from services.detection.src.detector import RawDetection
from services.detection.src.face_estimator import FaceEstimator
from services.detection.src.filtering import DetectionFilter, FilteredTrack
from services.detection.src.publisher import build_detection_event, build_track_result
from services.detection.src.tracker import CameraTracker, TrackerManager, _DetectionResults


class TestTracker(unittest.TestCase):

    @patch("services.detection.src.tracker.BYTETracker")
    def test_camera_tracker_init(self, mock_byte_tracker):
        mock_byte_tracker.return_value = MagicMock()
        tracker = CameraTracker(camera_id="cam-123", frame_rate=15)
        self.assertEqual(tracker.camera_id, "cam-123")
        self.assertEqual(tracker.frame_rate, 15)
        self.assertEqual(tracker._algorithm, "bytetrack")
        mock_byte_tracker.assert_called_once()

    @patch("services.detection.src.tracker.BYTETracker")
    def test_tracker_manager(self, mock_byte_tracker):
        mock_byte_tracker.return_value = MagicMock()
        manager = TrackerManager()
        t1 = manager.get("cam-1")
        t2 = manager.get("cam-1")
        self.assertIs(t1, t2)
        self.assertEqual(manager.camera_ids(), ["cam-1"])
        manager.drop("cam-1")
        self.assertEqual(manager.camera_ids(), [])

    def test_detection_results_adapter(self):
        detections = [
            RawDetection(x1=10, y1=20, x2=50, y2=80, confidence=0.9, class_id=0),
            RawDetection(x1=60, y1=70, x2=100, y2=150, confidence=0.8, class_id=0),
        ]
        results = _DetectionResults.from_detections(detections)
        self.assertEqual(len(results), 2)
        self.assertEqual(results.xyxy.shape, (2, 4))
        self.assertEqual(results.conf.shape, (2,))
        self.assertEqual(results.xywh.shape, (2, 4))

        empty_results = _DetectionResults.from_detections([])
        self.assertEqual(len(empty_results), 0)
        self.assertEqual(empty_results.xywh.shape, (0, 4))


class TestFaceEstimatorAndFilter(unittest.TestCase):

    def test_face_estimator(self):
        estimator = FaceEstimator(face_region_ratio=0.35, face_area_min=0.001)
        estimate = estimator.estimate(x1_norm=0.1, y1_norm=0.1, x2_norm=0.3, y2_norm=0.5)
        self.assertEqual(estimate.x1, 0.1)
        self.assertEqual(estimate.y1, 0.1)
        self.assertEqual(estimate.x2, 0.3)
        self.assertAlmostEqual(estimate.y2, 0.1 + (0.4 * 0.35))
        self.assertTrue(estimate.has_face)

    def test_detection_filter(self):
        from services.detection.src.tracker import TrackedDetection

        estimator = FaceEstimator()
        filter_obj = DetectionFilter(face_estimator=estimator, min_confidence=0.5)

        tracked = [
            # High confidence, valid box
            TrackedDetection(track_id=1, x1=100, y1=100, x2=200, y2=300, confidence=0.85, class_id=0),
            # Low confidence -> should be filtered out
            TrackedDetection(track_id=2, x1=100, y1=100, x2=200, y2=300, confidence=0.3, class_id=0),
            # Invalid / degenerate bbox -> should be filtered out
            TrackedDetection(track_id=3, x1=200, y1=300, x2=100, y2=100, confidence=0.9, class_id=0),
        ]

        filtered = filter_obj.apply(tracked, frame_width=1000, frame_height=1000)
        self.assertEqual(len(filtered), 1)
        self.assertEqual(filtered[0].track_id, 1)
        self.assertEqual(filtered[0].class_label, "person")
        self.assertAlmostEqual(filtered[0].bbox.x1, 0.1)
        self.assertAlmostEqual(filtered[0].bbox.y1, 0.1)
        self.assertAlmostEqual(filtered[0].bbox.x2, 0.2)
        self.assertAlmostEqual(filtered[0].bbox.y2, 0.3)


class TestPublisher(unittest.TestCase):

    def test_build_detection_event(self):
        frame_event = FrameEvent(
            camera_id=uuid4(),
            timestamp=datetime.now(timezone.utc),
            frame_seq=1,
            frame_reference="frame-001",
            frame_provider=FrameProvider.REDIS,
            frame_shape=(1080, 1920),
            profile=CameraProfile.BALANCED,
        )

        track = FilteredTrack(
            track_id=42,
            bbox=BoundingBox(x1=0.1, y1=0.2, x2=0.3, y2=0.4),
            confidence=0.92,
            class_label="person",
            has_face=True,
            face_bbox=BoundingBox(x1=0.1, y1=0.2, x2=0.3, y2=0.27),
        )

        detection_event = build_detection_event(
            frame_event=frame_event,
            tracks=[track],
            inference_latency_ms=12.5,
        )

        self.assertIsInstance(detection_event, DetectionEvent)
        self.assertEqual(detection_event.camera_id, frame_event.camera_id)
        self.assertEqual(detection_event.frame_seq, 1)
        self.assertEqual(detection_event.inference_latency_ms, 12.5)
        self.assertEqual(len(detection_event.tracks), 1)
        self.assertEqual(detection_event.tracks[0].track_id, 42)
        self.assertIsInstance(detection_event.tracks[0], TrackResult)


class TestDetectionConsumer(unittest.IsolatedAsyncioTestCase):

    @patch("services.detection.src.consumer.create_async_engine")
    def test_consumer_init_and_ack(self, mock_engine):
        from services.detection.src.consumer import DetectionConsumer

        consumer = DetectionConsumer()
        self.assertEqual(consumer.stream_key, f"frames:{settings.test_camera_id}")

    @patch("services.detection.src.consumer.create_async_engine")
    async def test_consumer_manual_ack(self, mock_engine):
        from services.detection.src.consumer import DetectionConsumer

        consumer = DetectionConsumer()
        consumer.redis = AsyncMock()

        await consumer.ack("1234-0")
        consumer.redis.xack.assert_called_once_with(
            consumer.stream_key, consumer.group_name, "1234-0"
        )

    @patch("services.detection.src.consumer.create_async_engine")
    async def test_consumer_process_with_ack_does_not_auto_ack(self, mock_engine):
        from services.detection.src.consumer import DetectionConsumer

        consumer = DetectionConsumer()
        consumer.redis = AsyncMock()
        consumer.process = AsyncMock()

        await consumer._process_with_ack("1234-0", {"data": "{}"})
        consumer.process.assert_called_once_with("1234-0", {"data": "{}"})
        # Verify redis.xack was NOT called prematurely
        consumer.redis.xack.assert_not_called()

    @patch("services.detection.src.consumer.create_async_engine")
    async def test_fetch_frame_key_prefix(self, mock_engine):
        from services.detection.src.consumer import DetectionConsumer

        consumer = DetectionConsumer()
        consumer._side_redis = AsyncMock()
        consumer._side_redis.get.return_value = b"raw_image_data"

        event = FrameEvent(
            camera_id=uuid4(),
            timestamp=datetime.now(timezone.utc),
            frame_seq=1,
            frame_reference="uuid-frame-123",
            frame_provider=FrameProvider.REDIS,
            frame_shape=(1080, 1920),
            profile=CameraProfile.BALANCED,
        )

        data = await consumer._fetch_frame(event)
        self.assertEqual(data, b"raw_image_data")
        consumer._side_redis.get.assert_called_once_with("frames:uuid-frame-123")

    @patch("services.detection.src.consumer.create_async_engine")
    async def test_consumer_stop_without_error(self, mock_engine):
        from services.detection.src.consumer import DetectionConsumer

        consumer = DetectionConsumer()
        consumer._batch_processor_task = asyncio.create_task(asyncio.sleep(10))
        consumer._side_redis = AsyncMock()
        consumer._engine = AsyncMock()

        await consumer.stop()
        self.assertTrue(consumer._stopping)
        self.assertTrue(consumer._batch_processor_task.cancelled())


if __name__ == "__main__":
    unittest.main()
