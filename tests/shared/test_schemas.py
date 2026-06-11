import unittest
from datetime import datetime, timezone
from uuid import UUID

from pydantic import ValidationError

from shared.schemas import (
    AlertEvent,
    AlertSeverity,
    AlertStatus,
    AlertType,
    BoundingBox,
    CameraProfile,
    CameraStatus,
    DataCategory,
    DensityLevel,
    DetectionEvent,
    FeedbackType,
    FrameEvent,
    IdentityTag,
    RecognitionEvent,
    TrackResult,
    ZoneEvent,
    ZoneEventType,
    ZoneType,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_bounding_box(**overrides):
    defaults = dict(x1=0.1, y1=0.1, x2=0.5, y2=0.5, confidence=0.9)
    defaults.update(overrides)
    return BoundingBox(**defaults)


def make_track_result(**overrides):
    defaults = dict(
        track_id=1,
        bbox=make_bounding_box(),
        frame_object_key="innovision-snapshots/frames/cam_01/000001.jpg",
    )
    defaults.update(overrides)
    return TrackResult(**defaults)


def make_frame_event(**overrides):
    defaults = dict(
        camera_id="cam_01",
        camera_profile=CameraProfile.BALANCED,
        frame_seq=1,
        frame_object_key="innovision-snapshots/frames/cam_01/000001.jpg",
        frame_shape=(1920, 1080),
    )
    defaults.update(overrides)
    return FrameEvent(**defaults)


def make_detection_event(**overrides):
    defaults = dict(
        camera_id="cam_01",
        camera_profile=CameraProfile.BALANCED,
        frame_object_key="innovision-snapshots/frames/cam_01/000001.jpg",
        frame_seq=1,
    )
    defaults.update(overrides)
    return DetectionEvent(**defaults)


def make_recognition_event(**overrides):
    defaults = dict(
        camera_id="cam_01",
        camera_profile=CameraProfile.HIGH_SECURITY,
        track_id=1,
        identity_tag=IdentityTag.ENROLLED,
        similarity_score=0.92,
        quality_score=0.88,
        frame_object_key="innovision-snapshots/frames/cam_01/000001.jpg",
    )
    defaults.update(overrides)
    return RecognitionEvent(**defaults)


def make_zone_event(**overrides):
    defaults = dict(
        zone_id="00000000-0000-0000-0000-000000000001",
        camera_id="cam_01",
        track_id=1,
        event_type=ZoneEventType.ENTERED,
    )
    defaults.update(overrides)
    return ZoneEvent(**defaults)


def make_alert_event(**overrides):
    defaults = dict(
        alert_type=AlertType.RESTRICTED_ENTRY,
        severity=AlertSeverity.HIGH,
        camera_id="cam_01",
    )
    defaults.update(overrides)
    return AlertEvent(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# BoundingBox Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBoundingBox(unittest.TestCase):

    def test_valid_box_creation(self):
        box = make_bounding_box()
        self.assertAlmostEqual(box.x1, 0.1)
        self.assertAlmostEqual(box.x2, 0.5)

    def test_width_height_area_center(self):
        box = make_bounding_box(x1=0.1, y1=0.2, x2=0.5, y2=0.6)
        self.assertAlmostEqual(box.width, 0.4)
        self.assertAlmostEqual(box.height, 0.4)
        self.assertAlmostEqual(box.area, 0.16)
        self.assertAlmostEqual(box.center[0], 0.3)
        self.assertAlmostEqual(box.center[1], 0.4)

    def test_x2_must_be_greater_than_x1(self):
        with self.assertRaises(ValidationError):
            make_bounding_box(x1=0.6, x2=0.2)

    def test_y2_must_be_greater_than_y1(self):
        with self.assertRaises(ValidationError):
            make_bounding_box(y1=0.7, y2=0.3)

    def test_equal_x1_x2_fails(self):
        with self.assertRaises(ValidationError):
            make_bounding_box(x1=0.4, x2=0.4)

    def test_coordinates_must_be_normalized(self):
        with self.assertRaises(ValidationError):
            make_bounding_box(x1=-0.1)
        with self.assertRaises(ValidationError):
            make_bounding_box(x2=1.1)

    def test_confidence_bounds(self):
        with self.assertRaises(ValidationError):
            make_bounding_box(confidence=1.5)
        with self.assertRaises(ValidationError):
            make_bounding_box(confidence=-0.1)

    def test_zero_to_one_edge_values_pass(self):
        box = make_bounding_box(x1=0.0, y1=0.0, x2=1.0, y2=1.0, confidence=1.0)
        self.assertAlmostEqual(box.area, 1.0)


# ─────────────────────────────────────────────────────────────────────────────
# TrackResult Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTrackResult(unittest.TestCase):

    def test_valid_track_result(self):
        track = make_track_result()
        self.assertEqual(track.track_id, 1)
        self.assertEqual(track.class_label, "person")
        self.assertFalse(track.has_face)

    def test_has_face_true(self):
        track = make_track_result(has_face=True)
        self.assertTrue(track.has_face)

    def test_custom_class_label(self):
        track = make_track_result(class_label="person")
        self.assertEqual(track.class_label, "person")

    def test_bbox_is_bounding_box_instance(self):
        track = make_track_result()
        self.assertIsInstance(track.bbox, BoundingBox)

    def test_missing_track_id_fails(self):
        with self.assertRaises(ValidationError):
            TrackResult(
                bbox=make_bounding_box(),
                frame_object_key="innovision-snapshots/frames/cam_01/000001.jpg",
            )

    def test_missing_frame_object_key_fails(self):
        with self.assertRaises(ValidationError):
            TrackResult(track_id=1, bbox=make_bounding_box())


# ─────────────────────────────────────────────────────────────────────────────
# FrameEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFrameEvent(unittest.TestCase):

    def test_valid_frame_event(self):
        event = make_frame_event()
        self.assertEqual(event.camera_id, "cam_01")
        self.assertEqual(event.frame_seq, 1)
        self.assertEqual(event.camera_profile, CameraProfile.BALANCED)
        self.assertIsInstance(event.event_id, UUID)
        self.assertIsInstance(event.timestamp, datetime)

    def test_timestamp_is_utc(self):
        event = make_frame_event()
        self.assertEqual(event.timestamp.tzinfo, timezone.utc)

    def test_negative_frame_seq_fails(self):
        with self.assertRaises(ValidationError):
            make_frame_event(frame_seq=-1)

    def test_zero_frame_seq_passes(self):
        event = make_frame_event(frame_seq=0)
        self.assertEqual(event.frame_seq, 0)

    def test_camera_shake_default_false(self):
        event = make_frame_event()
        self.assertFalse(event.camera_shake)

    def test_camera_shake_can_be_true(self):
        event = make_frame_event(camera_shake=True)
        self.assertTrue(event.camera_shake)

    def test_all_camera_profiles_accepted(self):
        for profile in CameraProfile:
            event = make_frame_event(camera_profile=profile)
            self.assertEqual(event.camera_profile, profile)

    def test_json_round_trip(self):
        event = make_frame_event()
        json_str = event.model_dump_json()
        restored = FrameEvent.model_validate_json(json_str)
        self.assertEqual(event.event_id, restored.event_id)
        self.assertEqual(event.camera_id, restored.camera_id)
        self.assertEqual(event.frame_seq, restored.frame_seq)
        self.assertEqual(event.camera_profile, restored.camera_profile)

    def test_missing_required_fields_fail(self):
        with self.assertRaises(ValidationError):
            FrameEvent(camera_profile=CameraProfile.BALANCED, frame_seq=1,
                       frame_object_key="key", frame_shape=(1920, 1080))
        with self.assertRaises(ValidationError):
            FrameEvent(camera_id="cam_01", camera_profile=CameraProfile.BALANCED,
                       frame_object_key="key", frame_shape=(1920, 1080))


# ─────────────────────────────────────────────────────────────────────────────
# DetectionEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectionEvent(unittest.TestCase):

    def test_valid_detection_event_no_tracks(self):
        event = make_detection_event()
        self.assertEqual(event.tracks, [])
        self.assertIsInstance(event.event_id, UUID)

    def test_detection_event_with_tracks(self):
        tracks = [make_track_result(track_id=i) for i in range(3)]
        event = make_detection_event(tracks=tracks)
        self.assertEqual(len(event.tracks), 3)
        self.assertEqual(event.tracks[0].track_id, 0)
        self.assertEqual(event.tracks[2].track_id, 2)

    def test_empty_tracks_is_valid(self):
        event = make_detection_event(tracks=[])
        self.assertEqual(len(event.tracks), 0)

    def test_json_round_trip_with_tracks(self):
        tracks = [make_track_result(track_id=1, has_face=True)]
        event = make_detection_event(tracks=tracks)
        restored = DetectionEvent.model_validate_json(event.model_dump_json())
        self.assertEqual(len(restored.tracks), 1)
        self.assertTrue(restored.tracks[0].has_face)


# ─────────────────────────────────────────────────────────────────────────────
# RecognitionEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRecognitionEvent(unittest.TestCase):

    def test_valid_enrolled_recognition(self):
        event = make_recognition_event()
        self.assertEqual(event.identity_tag, IdentityTag.ENROLLED)
        self.assertIsNone(event.person_id)
        self.assertIsNone(event.liveness_score)

    def test_visitor_tag(self):
        event = make_recognition_event(
            identity_tag=IdentityTag.VISITOR,
            similarity_score=0.68
        )
        self.assertEqual(event.identity_tag, IdentityTag.VISITOR)

    def test_unknown_tag(self):
        event = make_recognition_event(
            identity_tag=IdentityTag.UNKNOWN,
            similarity_score=0.45
        )
        self.assertEqual(event.identity_tag, IdentityTag.UNKNOWN)

    def test_similarity_score_out_of_range_fails(self):
        with self.assertRaises(ValidationError):
            make_recognition_event(similarity_score=1.5)
        with self.assertRaises(ValidationError):
            make_recognition_event(similarity_score=-0.1)

    def test_quality_score_out_of_range_fails(self):
        with self.assertRaises(ValidationError):
            make_recognition_event(quality_score=1.1)

    def test_liveness_score_optional(self):
        event = make_recognition_event(liveness_score=0.97)
        self.assertAlmostEqual(event.liveness_score, 0.97)

    def test_liveness_score_out_of_range_fails(self):
        with self.assertRaises(ValidationError):
            make_recognition_event(liveness_score=1.2)

    def test_json_round_trip(self):
        event = make_recognition_event(liveness_score=0.85)
        restored = RecognitionEvent.model_validate_json(event.model_dump_json())
        self.assertEqual(event.event_id, restored.event_id)
        self.assertAlmostEqual(restored.liveness_score, 0.85)


# ─────────────────────────────────────────────────────────────────────────────
# ZoneEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestZoneEvent(unittest.TestCase):

    def test_valid_entered_event(self):
        event = make_zone_event()
        self.assertEqual(event.event_type, ZoneEventType.ENTERED)
        self.assertIsNone(event.dwell_duration)
        self.assertIsNone(event.person_id)

    def test_exited_event(self):
        event = make_zone_event(event_type=ZoneEventType.EXITED)
        self.assertEqual(event.event_type, ZoneEventType.EXITED)

    def test_dwell_event_with_duration(self):
        event = make_zone_event(
            event_type=ZoneEventType.DWELL,
            dwell_duration=45.5
        )
        self.assertAlmostEqual(event.dwell_duration, 45.5)

    def test_negative_dwell_duration_fails(self):
        with self.assertRaises(ValidationError):
            make_zone_event(event_type=ZoneEventType.DWELL, dwell_duration=-1.0)

    def test_zone_id_is_uuid(self):
        event = make_zone_event()
        self.assertIsInstance(event.zone_id, UUID)

    def test_json_round_trip(self):
        event = make_zone_event(event_type=ZoneEventType.DWELL, dwell_duration=30.0)
        restored = ZoneEvent.model_validate_json(event.model_dump_json())
        self.assertEqual(event.event_id, restored.event_id)
        self.assertAlmostEqual(restored.dwell_duration, 30.0)


# ─────────────────────────────────────────────────────────────────────────────
# AlertEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAlertEvent(unittest.TestCase):

    def test_valid_alert_default_status(self):
        event = make_alert_event()
        self.assertEqual(event.status, AlertStatus.PENDING)
        self.assertIsNone(event.zone_id)
        self.assertIsNone(event.person_id)
        self.assertIsNone(event.similarity_score)
        self.assertIsNone(event.snapshot_object_key)

    def test_all_alert_types_accepted(self):
        for alert_type in AlertType:
            event = make_alert_event(alert_type=alert_type)
            self.assertEqual(event.alert_type, alert_type)

    def test_all_severity_levels_accepted(self):
        for severity in AlertSeverity:
            event = make_alert_event(severity=severity)
            self.assertEqual(event.severity, severity)

    def test_similarity_score_bounds(self):
        with self.assertRaises(ValidationError):
            make_alert_event(similarity_score=1.1)
        with self.assertRaises(ValidationError):
            make_alert_event(similarity_score=-0.5)

    def test_similarity_score_valid(self):
        event = make_alert_event(similarity_score=0.87)
        self.assertAlmostEqual(event.similarity_score, 0.87)

    def test_status_transitions(self):
        for status in AlertStatus:
            event = make_alert_event(status=status)
            self.assertEqual(event.status, status)

    def test_created_at_is_utc(self):
        event = make_alert_event()
        self.assertEqual(event.created_at.tzinfo, timezone.utc)

    def test_json_round_trip(self):
        event = make_alert_event(
            severity=AlertSeverity.CRITICAL,
            similarity_score=0.95,
            snapshot_object_key="innovision-snapshots/alerts/2024-01-01/alert_01.jpg"
        )
        restored = AlertEvent.model_validate_json(event.model_dump_json())
        self.assertEqual(event.event_id, restored.event_id)
        self.assertEqual(restored.severity, AlertSeverity.CRITICAL)
        self.assertAlmostEqual(restored.similarity_score, 0.95)


# ─────────────────────────────────────────────────────────────────────────────
# Enum completeness Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEnums(unittest.TestCase):

    def test_camera_profile_values(self):
        values = {p.value for p in CameraProfile}
        self.assertIn("high_security", values)
        self.assertIn("balanced", values)
        self.assertIn("high_throughput", values)
        self.assertIn("crowd_only", values)

    def test_identity_tag_values(self):
        values = {t.value for t in IdentityTag}
        self.assertIn("enrolled", values)
        self.assertIn("visitor", values)
        self.assertIn("unknown", values)

    def test_alert_type_values(self):
        values = {a.value for a in AlertType}
        self.assertIn("restricted_entry", values)
        self.assertIn("intruder", values)
        self.assertIn("headcount_breach", values)
        self.assertIn("crowd_density", values)
        self.assertIn("pedestrian_anomaly", values)

    def test_alert_severity_values(self):
        values = {s.value for s in AlertSeverity}
        self.assertIn("critical", values)
        self.assertIn("high", values)
        self.assertIn("medium", values)
        self.assertIn("low", values)

    def test_zone_event_type_values(self):
        values = {z.value for z in ZoneEventType}
        self.assertIn("entered", values)
        self.assertIn("exited", values)
        self.assertIn("dwell", values)

    def test_density_level_values(self):
        values = {d.value for d in DensityLevel}
        self.assertIn("low", values)
        self.assertIn("medium", values)
        self.assertIn("high", values)
        self.assertIn("critical", values)

    def test_camera_status_values(self):
        values = {s.value for s in CameraStatus}
        self.assertIn("online", values)
        self.assertIn("offline", values)
        self.assertIn("reconnecting", values)

    def test_feedback_type_values(self):
        values = {f.value for f in FeedbackType}
        self.assertIn("confirm", values)
        self.assertIn("reject", values)

    def test_data_category_values(self):
        values = {d.value for d in DataCategory}
        self.assertIn("visitor_embeddings", values)
        self.assertIn("recognition_events", values)
        self.assertIn("detection_events", values)
        self.assertIn("snapshots", values)
        self.assertIn("audit_log", values)

    def test_zone_type_values(self):
        values = {z.value for z in ZoneType}
        self.assertIn("restricted", values)
        self.assertIn("monitored", values)
        self.assertIn("safe", values)


if __name__ == "__main__":
    unittest.main(verbosity=2)