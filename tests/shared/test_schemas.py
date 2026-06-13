import unittest
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import ValidationError

from shared.schemas import (
    AlertEvent,
    AlertSeverity,
    AlertStatus,
    AlertType,
    BoundingBox,
    CameraProfile,
    CrowdModel,
    DensityLevel,
    DetectionEvent,
    EventType,
    FrameEvent,
    IdentityTag,
    OperatorRole,
    RecognitionEvent,
    TrackResult,
    ZoneEvent,
    ZoneType,
    CrowdFrameEvent,
)


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────

def make_bounding_box(**overrides):
    defaults = dict(x1=0.1, y1=0.1, x2=0.5, y2=0.5)
    defaults.update(overrides)
    return BoundingBox(**defaults)


def make_track_result(**overrides):
    defaults = dict(
        track_id=1,
        bbox=make_bounding_box(),
        confidence=0.9,
        class_label="person",
    )
    defaults.update(overrides)
    return TrackResult(**defaults)


def make_frame_event(**overrides):
    defaults = dict(
        camera_id=uuid4(),
        profile=CameraProfile.BALANCED,
        frame_seq=1,
        frame_object_key="innovision-snapshots/frames/cam_01/000001.jpg",
        frame_shape=(1920, 1080),
    )
    defaults.update(overrides)
    return FrameEvent(**defaults)


def make_detection_event(**overrides):
    defaults = dict(
        camera_id=uuid4(),
        frame_event_id=uuid4(),
        frame_object_key="innovision-snapshots/frames/cam_01/000001.jpg",
        frame_shape=(1920, 1080),
        profile=CameraProfile.BALANCED,
        inference_latency_ms=12.5,
    )
    defaults.update(overrides)
    return DetectionEvent(**defaults)


def make_recognition_event(**overrides):
    defaults = dict(
        camera_id=uuid4(),
        detection_event_id=uuid4(),
        track_id=1,
        identity_tag=IdentityTag.ENROLLED,
        similarity_score=0.92,
        quality_score=0.88,
    )
    defaults.update(overrides)
    return RecognitionEvent(**defaults)


def make_zone_event(**overrides):
    defaults = dict(
        camera_id=uuid4(),
        zone_id=uuid4(),
        track_id=1,
        event_type="entered",
    )
    defaults.update(overrides)
    return ZoneEvent(**defaults)


def make_crowd_frame_event(**overrides):
    defaults = dict(
        camera_id=uuid4(),
        frame_object_key="innovision-snapshots/frames/cam_01/000001.jpg",
        frame_shape=(1920, 1080),
        crowd_model=CrowdModel.CSRNET,
        zone_ids=[uuid4()],
    )
    defaults.update(overrides)
    return CrowdFrameEvent(**defaults)


def make_alert_event(**overrides):
    defaults = dict(
        alert_type=AlertType.RESTRICTED_ENTRY,
        severity=AlertSeverity.HIGH,
        camera_id=uuid4(),
        track_id=1,
        confidence=0.91,
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

    def test_centroid(self):
        box = make_bounding_box(x1=0.1, y1=0.2, x2=0.5, y2=0.6)
        self.assertAlmostEqual(box.centroid[0], 0.3)
        self.assertAlmostEqual(box.centroid[1], 0.4)

    def test_area(self):
        box = make_bounding_box(x1=0.1, y1=0.2, x2=0.5, y2=0.6)
        self.assertAlmostEqual(box.area, 0.16)

    def test_coordinates_must_be_normalized(self):
        with self.assertRaises(ValidationError):
            make_bounding_box(x1=-0.1)
        with self.assertRaises(ValidationError):
            make_bounding_box(x2=1.1)

    def test_zero_to_one_edge_values_pass(self):
        box = make_bounding_box(x1=0.0, y1=0.0, x2=1.0, y2=1.0)
        self.assertAlmostEqual(box.area, 1.0)

    def test_no_confidence_field(self):
        box = make_bounding_box()
        self.assertFalse(hasattr(box, "confidence"))


# ─────────────────────────────────────────────────────────────────────────────
# TrackResult Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTrackResult(unittest.TestCase):

    def test_valid_track_result(self):
        track = make_track_result()
        self.assertEqual(track.track_id, 1)
        self.assertEqual(track.class_label, "person")
        self.assertFalse(track.has_face)
        self.assertIsNone(track.face_bbox)

    def test_confidence_field_present(self):
        track = make_track_result(confidence=0.85)
        self.assertAlmostEqual(track.confidence, 0.85)

    def test_confidence_out_of_range_fails(self):
        with self.assertRaises(ValidationError):
            make_track_result(confidence=1.5)
        with self.assertRaises(ValidationError):
            make_track_result(confidence=-0.1)

    def test_has_face_true(self):
        track = make_track_result(has_face=True)
        self.assertTrue(track.has_face)

    def test_face_bbox_optional(self):
        face = make_bounding_box(x1=0.2, y1=0.2, x2=0.4, y2=0.4)
        track = make_track_result(has_face=True, face_bbox=face)
        self.assertIsInstance(track.face_bbox, BoundingBox)

    def test_bbox_is_bounding_box_instance(self):
        track = make_track_result()
        self.assertIsInstance(track.bbox, BoundingBox)

    def test_missing_track_id_fails(self):
        with self.assertRaises(ValidationError):
            TrackResult(bbox=make_bounding_box(), confidence=0.9, class_label="person")

    def test_missing_confidence_fails(self):
        with self.assertRaises(ValidationError):
            TrackResult(track_id=1, bbox=make_bounding_box(), class_label="person")


# ─────────────────────────────────────────────────────────────────────────────
# FrameEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFrameEvent(unittest.TestCase):

    def test_valid_frame_event(self):
        event = make_frame_event()
        self.assertIsInstance(event.camera_id, UUID)
        self.assertEqual(event.frame_seq, 1)
        self.assertEqual(event.profile, CameraProfile.BALANCED)
        self.assertIsInstance(event.event_id, UUID)
        self.assertIsInstance(event.timestamp, datetime)

    def test_camera_id_is_uuid(self):
        event = make_frame_event()
        self.assertIsInstance(event.camera_id, UUID)

    def test_timestamp_is_utc(self):
        event = make_frame_event()
        self.assertEqual(event.timestamp.tzinfo, timezone.utc)

    def test_negative_frame_seq_fails(self):
        with self.assertRaises(ValidationError):
            make_frame_event(frame_seq=-1)

    def test_zero_frame_seq_passes(self):
        event = make_frame_event(frame_seq=0)
        self.assertEqual(event.frame_seq, 0)

    def test_all_camera_profiles_accepted(self):
        for profile in CameraProfile:
            event = make_frame_event(profile=profile)
            self.assertEqual(event.profile, profile)

    def test_no_camera_shake_field(self):
        event = make_frame_event()
        self.assertFalse(hasattr(event, "camera_shake"))

    def test_json_round_trip(self):
        event = make_frame_event()
        restored = FrameEvent.model_validate_json(event.model_dump_json())
        self.assertEqual(event.event_id, restored.event_id)
        self.assertEqual(event.camera_id, restored.camera_id)
        self.assertEqual(event.frame_seq, restored.frame_seq)
        self.assertEqual(event.profile, restored.profile)

    def test_missing_camera_id_fails(self):
        with self.assertRaises(ValidationError):
            FrameEvent(
                profile=CameraProfile.BALANCED,
                frame_seq=1,
                frame_object_key="key",
                frame_shape=(1920, 1080),
            )


# ─────────────────────────────────────────────────────────────────────────────
# DetectionEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectionEvent(unittest.TestCase):

    def test_valid_detection_event_no_tracks(self):
        event = make_detection_event()
        self.assertEqual(event.tracks, [])
        self.assertIsInstance(event.event_id, UUID)

    def test_camera_id_is_uuid(self):
        event = make_detection_event()
        self.assertIsInstance(event.camera_id, UUID)

    def test_frame_event_id_is_uuid(self):
        event = make_detection_event()
        self.assertIsInstance(event.frame_event_id, UUID)

    def test_inference_latency_present(self):
        event = make_detection_event(inference_latency_ms=25.3)
        self.assertAlmostEqual(event.inference_latency_ms, 25.3)

    def test_detection_event_with_tracks(self):
        tracks = [make_track_result(track_id=i) for i in range(3)]
        event = make_detection_event(tracks=tracks)
        self.assertEqual(len(event.tracks), 3)

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
        self.assertFalse(event.liveness_checked)

    def test_detection_event_id_present(self):
        event = make_recognition_event()
        self.assertIsInstance(event.detection_event_id, UUID)

    def test_liveness_checked_flag(self):
        event = make_recognition_event(liveness_checked=True, liveness_score=0.97)
        self.assertTrue(event.liveness_checked)
        self.assertAlmostEqual(event.liveness_score, 0.97)

    def test_visitor_tag(self):
        event = make_recognition_event(
            identity_tag=IdentityTag.VISITOR, similarity_score=0.68
        )
        self.assertEqual(event.identity_tag, IdentityTag.VISITOR)

    def test_unknown_tag(self):
        event = make_recognition_event(
            identity_tag=IdentityTag.UNKNOWN, similarity_score=0.45
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

    def test_liveness_score_out_of_range_fails(self):
        with self.assertRaises(ValidationError):
            make_recognition_event(liveness_score=1.2)

    def test_json_round_trip(self):
        event = make_recognition_event(liveness_score=0.85, liveness_checked=True)
        restored = RecognitionEvent.model_validate_json(event.model_dump_json())
        self.assertEqual(event.event_id, restored.event_id)
        self.assertAlmostEqual(restored.liveness_score, 0.85)
        self.assertTrue(restored.liveness_checked)


# ─────────────────────────────────────────────────────────────────────────────
# ZoneEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestZoneEvent(unittest.TestCase):

    def test_valid_entered_event(self):
        event = make_zone_event()
        self.assertEqual(event.event_type, "entered")
        self.assertIsNone(event.dwell_duration_seconds)
        self.assertIsNone(event.person_id)
        self.assertIsNone(event.global_id)

    def test_camera_id_is_uuid(self):
        event = make_zone_event()
        self.assertIsInstance(event.camera_id, UUID)

    def test_exited_event(self):
        event = make_zone_event(event_type="exited")
        self.assertEqual(event.event_type, "exited")

    def test_dwell_event_with_duration(self):
        event = make_zone_event(event_type="dwell", dwell_duration_seconds=45.5)
        self.assertAlmostEqual(event.dwell_duration_seconds, 45.5)

    def test_negative_dwell_duration_fails(self):
        with self.assertRaises(ValidationError):
            make_zone_event(dwell_duration_seconds=-1.0)

    def test_global_id_optional(self):
        gid = uuid4()
        event = make_zone_event(global_id=gid)
        self.assertEqual(event.global_id, gid)

    def test_json_round_trip(self):
        event = make_zone_event(event_type="dwell", dwell_duration_seconds=30.0)
        restored = ZoneEvent.model_validate_json(event.model_dump_json())
        self.assertEqual(event.event_id, restored.event_id)
        self.assertAlmostEqual(restored.dwell_duration_seconds, 30.0)


# ─────────────────────────────────────────────────────────────────────────────
# CrowdFrameEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCrowdFrameEvent(unittest.TestCase):

    def test_valid_crowd_frame_event(self):
        event = make_crowd_frame_event()
        self.assertIsInstance(event.camera_id, UUID)
        self.assertEqual(event.crowd_model, CrowdModel.CSRNET)
        self.assertEqual(len(event.zone_ids), 1)

    def test_all_crowd_models_accepted(self):
        for model in CrowdModel:
            event = make_crowd_frame_event(crowd_model=model)
            self.assertEqual(event.crowd_model, model)

    def test_multiple_zone_ids(self):
        zone_ids = [uuid4() for _ in range(3)]
        event = make_crowd_frame_event(zone_ids=zone_ids)
        self.assertEqual(len(event.zone_ids), 3)

    def test_json_round_trip(self):
        event = make_crowd_frame_event()
        restored = CrowdFrameEvent.model_validate_json(event.model_dump_json())
        self.assertEqual(event.event_id, restored.event_id)
        self.assertEqual(event.crowd_model, restored.crowd_model)


# ─────────────────────────────────────────────────────────────────────────────
# AlertEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAlertEvent(unittest.TestCase):

    def test_valid_alert_defaults(self):
        event = make_alert_event()
        self.assertIsNone(event.zone_id)
        self.assertIsNone(event.person_id)
        self.assertIsNone(event.similarity_score)
        self.assertIsNone(event.snapshot_object_key)
        self.assertIsNone(event.global_id)
        self.assertFalse(event.requires_human_verification)
        self.assertEqual(event.metadata, {})

    def test_camera_id_is_uuid(self):
        event = make_alert_event()
        self.assertIsInstance(event.camera_id, UUID)

    def test_track_id_required(self):
        with self.assertRaises(ValidationError):
            AlertEvent(
                alert_type=AlertType.RESTRICTED_ENTRY,
                severity=AlertSeverity.HIGH,
                camera_id=uuid4(),
                confidence=0.9,
            )

    def test_confidence_required(self):
        with self.assertRaises(ValidationError):
            AlertEvent(
                alert_type=AlertType.RESTRICTED_ENTRY,
                severity=AlertSeverity.HIGH,
                camera_id=uuid4(),
                track_id=1,
            )

    def test_confidence_bounds(self):
        with self.assertRaises(ValidationError):
            make_alert_event(confidence=1.1)
        with self.assertRaises(ValidationError):
            make_alert_event(confidence=-0.1)

    def test_all_alert_types_accepted(self):
        for alert_type in AlertType:
            event = make_alert_event(alert_type=alert_type)
            self.assertEqual(event.alert_type, alert_type)

    def test_all_severity_levels_accepted(self):
        for severity in AlertSeverity:
            event = make_alert_event(severity=severity)
            self.assertEqual(event.severity, severity)

    def test_requires_human_verification(self):
        event = make_alert_event(requires_human_verification=True)
        self.assertTrue(event.requires_human_verification)

    def test_metadata_dict(self):
        event = make_alert_event(metadata={"source": "zone_monitor", "count": 3})
        self.assertEqual(event.metadata["source"], "zone_monitor")

    def test_similarity_score_bounds(self):
        with self.assertRaises(ValidationError):
            make_alert_event(similarity_score=1.1)
        with self.assertRaises(ValidationError):
            make_alert_event(similarity_score=-0.5)

    def test_timestamp_is_utc(self):
        event = make_alert_event()
        self.assertEqual(event.timestamp.tzinfo, timezone.utc)

    def test_json_round_trip(self):
        event = make_alert_event(
            severity=AlertSeverity.CRITICAL,
            similarity_score=0.95,
            requires_human_verification=True,
            metadata={"reason": "high_similarity"},
        )
        restored = AlertEvent.model_validate_json(event.model_dump_json())
        self.assertEqual(event.event_id, restored.event_id)
        self.assertEqual(restored.severity, AlertSeverity.CRITICAL)
        self.assertTrue(restored.requires_human_verification)
        self.assertEqual(restored.metadata["reason"], "high_similarity")


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

    def test_event_type_values(self):
        values = {z.value for z in EventType}
        self.assertIn("entered", values)
        self.assertIn("exited", values)
        self.assertIn("dwell", values)

    def test_crowd_model_values(self):
        values = {c.value for c in CrowdModel}
        self.assertIn("csrnet", values)
        self.assertIn("dmcount", values)
        self.assertIn("crowdformer", values)

    def test_density_level_values(self):
        values = {d.value for d in DensityLevel}
        self.assertIn("low", values)
        self.assertIn("medium", values)
        self.assertIn("high", values)
        self.assertIn("critical", values)

    def test_operator_role_values(self):
        values = {o.value for o in OperatorRole}
        self.assertIn("superadmin", values)
        self.assertIn("admin", values)
        self.assertIn("operator", values)
        self.assertIn("viewer", values)

    def test_zone_type_values(self):
        values = {z.value for z in ZoneType}
        self.assertIn("restricted", values)
        self.assertIn("monitored", values)
        self.assertIn("safe", values)


if __name__ == "__main__":
    unittest.main(verbosity=2)