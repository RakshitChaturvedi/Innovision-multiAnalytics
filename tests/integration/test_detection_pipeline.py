import unittest
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch
from uuid import uuid4

import cv2
import numpy as np

from services.detection.src.consumer import DetectionConsumer
from services.detection.src.detector import RawDetection
from shared.schemas.enums import CameraProfile, FrameProvider
from shared.schemas.events import FrameEvent


class TestDetectionPipelineIntegration(unittest.IsolatedAsyncioTestCase):
    """
    End-to-End Integration Test for the Detection Pipeline.

    Verifies the complete dataflow:
      Redis Stream message -> FrameEvent validation -> Redis frame fetch ->
      OpenCV decode -> BatchManager -> YOLODetector inference ->
      ByteTrack CameraTracker -> DetectionFilter -> DetectionPublisher ->
      PostgreSQL batch insert -> Redis XACK
    """

    @patch("services.detection.src.consumer.create_async_engine")
    async def test_full_detection_pipeline_flow(self, mock_engine):
        camera_id = uuid4()
        frame_ref = "test-frame-001"

        # 1. Create a synthetic test image (BGR 640x480)
        img = np.zeros((480, 640, 3), dtype=np.uint8)
        # Draw a white rectangle simulating a detected subject
        cv2.rectangle(img, (100, 100), (300, 400), (255, 255, 255), -1)
        _, encoded_bytes = cv2.imencode(".jpg", img)
        frame_bytes = encoded_bytes.tobytes()

        # 2. Build the incoming FrameEvent
        frame_event = FrameEvent(
            camera_id=camera_id,
            timestamp=datetime.now(UTC),
            frame_seq=1,
            frame_reference=frame_ref,
            frame_provider=FrameProvider.REDIS,
            frame_shape=(480, 640),
            profile=CameraProfile.BALANCED,
        )

        # 3. Instantiate DetectionConsumer
        consumer = DetectionConsumer()

        # Mock external connections (Redis and DB session)
        redis_mock = AsyncMock()
        consumer.redis = redis_mock
        consumer._side_redis = redis_mock

        # Setup Redis frame cache mock
        redis_mock.get.return_value = frame_bytes

        # Mock detector to simulate YOLO detecting a person
        mock_detector = MagicMock()
        mock_detector.run_batch.return_value = (
            [[RawDetection(x1=100.0, y1=100.0, x2=300.0, y2=400.0, confidence=0.92, class_id=0)]],
            15.4,  # latency_ms
        )
        consumer._detector = mock_detector

        # Mock publisher
        mock_publisher = AsyncMock()
        consumer._publisher = mock_publisher

        # Mock database session
        mock_session = AsyncMock()
        mock_begin_ctx = MagicMock()
        mock_begin_ctx.__aenter__ = AsyncMock(return_value=None)
        mock_begin_ctx.__aexit__ = AsyncMock(return_value=None)
        mock_session.begin = MagicMock(return_value=mock_begin_ctx)

        mock_session_ctx = MagicMock()
        mock_session_ctx.__aenter__ = AsyncMock(return_value=mock_session)
        mock_session_ctx.__aexit__ = AsyncMock(return_value=None)
        consumer._session_factory = MagicMock(return_value=mock_session_ctx)

        # 4. Start batch manager
        await consumer._batch_manager.start()

        # 5. Send message through consumer.process()
        msg_id = "1700000000000-0"
        msg_payload = {"data": frame_event.model_dump_json()}
        await consumer._process_with_ack(msg_id, msg_payload)

        # Verify frame was fetched from Redis using proper frames: prefix
        redis_mock.get.assert_called_with(f"frames:{frame_ref}")

        # Verify it was NOT prematurely acked on enqueue
        redis_mock.xack.assert_not_called()

        # 6. Retrieve ready batch from BatchManager
        batch = await consumer._batch_manager.get_ready_batch()
        self.assertEqual(len(batch.items), 1)

        # 7. Process batch through YOLO, Tracker, Filter, Publisher, and DB
        await consumer._process_batch(batch)

        # Verify DetectionPublisher was called with published tracks
        mock_publisher.publish.assert_called_once()
        publish_call_kwargs = mock_publisher.publish.call_args.kwargs
        self.assertEqual(publish_call_kwargs["frame_event"].camera_id, camera_id)
        self.assertEqual(len(publish_call_kwargs["tracks"]), 1)
        self.assertEqual(publish_call_kwargs["tracks"][0].class_label, "person")
        self.assertAlmostEqual(publish_call_kwargs["inference_latency_ms"], 15.4)

        # Verify DB insert was executed with correctly formatted row
        mock_session.execute.assert_called_once()
        db_args = mock_session.execute.call_args[0]
        db_rows = db_args[1]
        self.assertEqual(len(db_rows), 1)
        self.assertEqual(db_rows[0]["camera_id"], str(camera_id))
        self.assertEqual(db_rows[0]["frame_seq"], 1)
        self.assertEqual(db_rows[0]["class_label"], "person")
        self.assertTrue(db_rows[0]["has_face"])

        # Verify message was ACKed in Redis after full pipeline completion
        redis_mock.xack.assert_called_once_with(
            consumer.stream_key, consumer.group_name, msg_id
        )

        # 8. Clean up
        await consumer._batch_manager.stop()


if __name__ == "__main__":
    unittest.main()
