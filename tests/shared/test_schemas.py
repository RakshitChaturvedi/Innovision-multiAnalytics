import pytest
from datetime import datetime, timezone
from uuid import UUID, uuid4

from pydantic import ValidationError

from shared.schemas import (
    AlertEvent,
    AlertSeverity,
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


def utcnow():
    return datetime.now(timezone.utc)


def make_frame_event(**overrides):
    defaults = dict(
        camera_id=uuid4(),
        profile=CameraProfile.BALANCED,
        frame_seq=1,
        frame_object_key="innovision-snapshots/frames/cam_01/000001.jpg",
        frame_shape=(1920, 1080),
        timestamp=utcnow(),
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
        tracks=[],
        timestamp=utcnow(),
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
        timestamp=utcnow(),
    )
    defaults.update(overrides)
    return RecognitionEvent(**defaults)


def make_zone_event(**overrides):
    defaults = dict(
        camera_id=uuid4(),
        zone_id=uuid4(),
        track_id=1,
        event_type="entered",
        timestamp=utcnow(),
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
        timestamp=utcnow(),
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
        timestamp=utcnow(),
    )
    defaults.update(overrides)
    return AlertEvent(**defaults)


# ─────────────────────────────────────────────────────────────────────────────
# BoundingBox Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestBoundingBox:

    def test_valid_box_creation(self):
        box = make_bounding_box()
        assert box.x1 == pytest.approx(0.1)
        assert box.x2 == pytest.approx(0.5)

    def test_centroid(self):
        box = make_bounding_box(x1=0.1, y1=0.2, x2=0.5, y2=0.6)
        assert box.centroid[0] == pytest.approx(0.3)
        assert box.centroid[1] == pytest.approx(0.4)

    def test_area(self):
        box = make_bounding_box(x1=0.1, y1=0.2, x2=0.5, y2=0.6)
        assert box.area == pytest.approx(0.16)

    def test_coordinates_below_zero_fails(self):
        with pytest.raises(ValidationError):
            make_bounding_box(x1=-0.1)

    def test_coordinates_above_one_fails(self):
        with pytest.raises(ValidationError):
            make_bounding_box(x2=1.1)

    def test_zero_to_one_edge_values_pass(self):
        box = make_bounding_box(x1=0.0, y1=0.0, x2=1.0, y2=1.0)
        assert box.area == pytest.approx(1.0)

    def test_no_confidence_field(self):
        box = make_bounding_box()
        assert not hasattr(box, "confidence")


# ─────────────────────────────────────────────────────────────────────────────
# TrackResult Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestTrackResult:

    def test_valid_track_result(self):
        track = make_track_result()
        assert track.track_id == 1
        assert track.class_label == "person"
        assert track.has_face is False
        assert track.face_bbox is None

    def test_confidence_field_present(self):
        track = make_track_result(confidence=0.85)
        assert track.confidence == pytest.approx(0.85)

    def test_confidence_above_one_fails(self):
        with pytest.raises(ValidationError):
            make_track_result(confidence=1.5)

    def test_confidence_below_zero_fails(self):
        with pytest.raises(ValidationError):
            make_track_result(confidence=-0.1)

    def test_has_face_true(self):
        track = make_track_result(has_face=True)
        assert track.has_face is True

    def test_face_bbox_optional(self):
        face = make_bounding_box(x1=0.2, y1=0.2, x2=0.4, y2=0.4)
        track = make_track_result(has_face=True, face_bbox=face)
        assert isinstance(track.face_bbox, BoundingBox)

    def test_bbox_is_bounding_box_instance(self):
        track = make_track_result()
        assert isinstance(track.bbox, BoundingBox)

    def test_missing_track_id_fails(self):
        with pytest.raises(ValidationError):
            TrackResult(bbox=make_bounding_box(), confidence=0.9, class_label="person")

    def test_missing_confidence_fails(self):
        with pytest.raises(ValidationError):
            TrackResult(track_id=1, bbox=make_bounding_box(), class_label="person")


# ─────────────────────────────────────────────────────────────────────────────
# FrameEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestFrameEvent:

    def test_valid_frame_event(self):
        event = make_frame_event()
        assert isinstance(event.camera_id, UUID)
        assert event.frame_seq == 1
        assert event.profile == CameraProfile.BALANCED
        assert isinstance(event.event_id, UUID)
        assert isinstance(event.timestamp, datetime)

    def test_camera_id_is_uuid(self):
        event = make_frame_event()
        assert isinstance(event.camera_id, UUID)

    def test_timestamp_required(self):
        with pytest.raises(ValidationError):
            FrameEvent(
                camera_id=uuid4(),
                profile=CameraProfile.BALANCED,
                frame_seq=1,
                frame_object_key="key",
                frame_shape=(1920, 1080),
            )

    def test_negative_frame_seq_fails(self):
        with pytest.raises(ValidationError):
            make_frame_event(frame_seq=-1)

    def test_zero_frame_seq_passes(self):
        event = make_frame_event(frame_seq=0)
        assert event.frame_seq == 0

    def test_all_camera_profiles_accepted(self):
        for profile in CameraProfile:
            event = make_frame_event(profile=profile)
            assert event.profile == profile

    def test_no_camera_shake_field(self):
        event = make_frame_event()
        assert not hasattr(event, "camera_shake")

    def test_json_round_trip(self):
        event = make_frame_event()
        restored = FrameEvent.model_validate_json(event.model_dump_json())
        assert event.event_id == restored.event_id
        assert event.camera_id == restored.camera_id
        assert event.frame_seq == restored.frame_seq
        assert event.profile == restored.profile

    def test_missing_camera_id_fails(self):
        with pytest.raises(ValidationError):
            FrameEvent(
                profile=CameraProfile.BALANCED,
                frame_seq=1,
                frame_object_key="key",
                frame_shape=(1920, 1080),
                timestamp=utcnow(),
            )


# ─────────────────────────────────────────────────────────────────────────────
# DetectionEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestDetectionEvent:

    def test_valid_detection_event_no_tracks(self):
        event = make_detection_event()
        assert event.tracks == []
        assert isinstance(event.event_id, UUID)

    def test_camera_id_is_uuid(self):
        event = make_detection_event()
        assert isinstance(event.camera_id, UUID)

    def test_frame_event_id_is_uuid(self):
        event = make_detection_event()
        assert isinstance(event.frame_event_id, UUID)

    def test_inference_latency_present(self):
        event = make_detection_event(inference_latency_ms=25.3)
        assert event.inference_latency_ms == pytest.approx(25.3)

    def test_timestamp_required(self):
        with pytest.raises(ValidationError):
            DetectionEvent(
                camera_id=uuid4(),
                frame_event_id=uuid4(),
                frame_object_key="key",
                frame_shape=(1920, 1080),
                profile=CameraProfile.BALANCED,
                inference_latency_ms=10.0,
                tracks=[],
            )

    def test_detection_event_with_tracks(self):
        tracks = [make_track_result(track_id=i) for i in range(3)]
        event = make_detection_event(tracks=tracks)
        assert len(event.tracks) == 3

    def test_empty_tracks_is_valid(self):
        event = make_detection_event(tracks=[])
        assert len(event.tracks) == 0

    def test_json_round_trip_with_tracks(self):
        tracks = [make_track_result(track_id=1, has_face=True)]
        event = make_detection_event(tracks=tracks)
        restored = DetectionEvent.model_validate_json(event.model_dump_json())
        assert len(restored.tracks) == 1
        assert restored.tracks[0].has_face is True


# ─────────────────────────────────────────────────────────────────────────────
# RecognitionEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestRecognitionEvent:

    def test_valid_enrolled_recognition(self):
        event = make_recognition_event()
        assert event.identity_tag == IdentityTag.ENROLLED
        assert event.person_id is None
        assert event.liveness_score is None
        assert event.liveness_checked is False

    def test_detection_event_id_present(self):
        event = make_recognition_event()
        assert isinstance(event.detection_event_id, UUID)

    def test_liveness_checked_flag(self):
        event = make_recognition_event(liveness_checked=True, liveness_score=0.97)
        assert event.liveness_checked is True
        assert event.liveness_score == pytest.approx(0.97)

    def test_visitor_tag(self):
        event = make_recognition_event(
            identity_tag=IdentityTag.VISITOR, similarity_score=0.68
        )
        assert event.identity_tag == IdentityTag.VISITOR

    def test_unknown_tag(self):
        event = make_recognition_event(
            identity_tag=IdentityTag.UNKNOWN, similarity_score=0.45
        )
        assert event.identity_tag == IdentityTag.UNKNOWN

    def test_similarity_score_above_one_fails(self):
        with pytest.raises(ValidationError):
            make_recognition_event(similarity_score=1.5)

    def test_similarity_score_below_zero_fails(self):
        with pytest.raises(ValidationError):
            make_recognition_event(similarity_score=-0.1)

    def test_quality_score_out_of_range_fails(self):
        with pytest.raises(ValidationError):
            make_recognition_event(quality_score=1.1)

    def test_timestamp_required(self):
        with pytest.raises(ValidationError):
            RecognitionEvent(
                camera_id=uuid4(),
                detection_event_id=uuid4(),
                track_id=1,
                identity_tag=IdentityTag.ENROLLED,
                similarity_score=0.92,
                quality_score=0.88,
            )

    def test_json_round_trip(self):
        event = make_recognition_event(liveness_score=0.85, liveness_checked=True)
        restored = RecognitionEvent.model_validate_json(event.model_dump_json())
        assert event.event_id == restored.event_id
        assert restored.liveness_score == pytest.approx(0.85)
        assert restored.liveness_checked is True


# ─────────────────────────────────────────────────────────────────────────────
# ZoneEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestZoneEvent:

    def test_valid_entered_event(self):
        event = make_zone_event()
        assert event.event_type == "entered"
        assert event.dwell_duration_seconds is None
        assert event.person_id is None
        assert event.global_id is None

    def test_camera_id_is_uuid(self):
        event = make_zone_event()
        assert isinstance(event.camera_id, UUID)

    def test_exited_event(self):
        event = make_zone_event(event_type="exited")
        assert event.event_type == "exited"

    def test_dwell_event_with_duration(self):
        event = make_zone_event(event_type="dwell", dwell_duration_seconds=45.5)
        assert event.dwell_duration_seconds == pytest.approx(45.5)

    def test_global_id_optional(self):
        gid = uuid4()
        event = make_zone_event(global_id=gid)
        assert event.global_id == gid

    def test_timestamp_required(self):
        with pytest.raises(ValidationError):
            ZoneEvent(
                camera_id=uuid4(),
                zone_id=uuid4(),
                track_id=1,
                event_type="entered",
            )

    def test_json_round_trip(self):
        event = make_zone_event(event_type="dwell", dwell_duration_seconds=30.0)
        restored = ZoneEvent.model_validate_json(event.model_dump_json())
        assert event.event_id == restored.event_id
        assert restored.dwell_duration_seconds == pytest.approx(30.0)


# ─────────────────────────────────────────────────────────────────────────────
# CrowdFrameEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestCrowdFrameEvent:

    def test_valid_crowd_frame_event(self):
        event = make_crowd_frame_event()
        assert isinstance(event.camera_id, UUID)
        assert event.crowd_model == CrowdModel.CSRNET
        assert len(event.zone_ids) == 1

    def test_all_crowd_models_accepted(self):
        for model in CrowdModel:
            event = make_crowd_frame_event(crowd_model=model)
            assert event.crowd_model == model

    def test_multiple_zone_ids(self):
        zone_ids = [uuid4() for _ in range(3)]
        event = make_crowd_frame_event(zone_ids=zone_ids)
        assert len(event.zone_ids) == 3

    def test_timestamp_required(self):
        with pytest.raises(ValidationError):
            CrowdFrameEvent(
                camera_id=uuid4(),
                frame_object_key="key",
                frame_shape=(1920, 1080),
                crowd_model=CrowdModel.CSRNET,
                zone_ids=[uuid4()],
            )

    def test_json_round_trip(self):
        event = make_crowd_frame_event()
        restored = CrowdFrameEvent.model_validate_json(event.model_dump_json())
        assert event.event_id == restored.event_id
        assert event.crowd_model == restored.crowd_model


# ─────────────────────────────────────────────────────────────────────────────
# AlertEvent Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestAlertEvent:

    def test_valid_alert_defaults(self):
        event = make_alert_event()
        assert event.zone_id is None
        assert event.person_id is None
        assert event.similarity_score is None
        assert event.snapshot_object_key is None
        assert event.global_id is None
        assert event.requires_human_verification is False
        assert event.metadata == {}

    def test_camera_id_is_uuid(self):
        event = make_alert_event()
        assert isinstance(event.camera_id, UUID)

    def test_track_id_required(self):
        with pytest.raises(ValidationError):
            AlertEvent(
                alert_type=AlertType.RESTRICTED_ENTRY,
                severity=AlertSeverity.HIGH,
                camera_id=uuid4(),
                confidence=0.9,
                timestamp=utcnow(),
            )

    def test_confidence_required(self):
        with pytest.raises(ValidationError):
            AlertEvent(
                alert_type=AlertType.RESTRICTED_ENTRY,
                severity=AlertSeverity.HIGH,
                camera_id=uuid4(),
                track_id=1,
                timestamp=utcnow(),
            )

    def test_timestamp_required(self):
        with pytest.raises(ValidationError):
            AlertEvent(
                alert_type=AlertType.RESTRICTED_ENTRY,
                severity=AlertSeverity.HIGH,
                camera_id=uuid4(),
                track_id=1,
                confidence=0.9,
            )

    def test_confidence_above_one_fails(self):
        with pytest.raises(ValidationError):
            make_alert_event(confidence=1.1)

    def test_confidence_below_zero_fails(self):
        with pytest.raises(ValidationError):
            make_alert_event(confidence=-0.1)

    def test_all_alert_types_accepted(self):
        for alert_type in AlertType:
            event = make_alert_event(alert_type=alert_type)
            assert event.alert_type == alert_type

    def test_all_severity_levels_accepted(self):
        for severity in AlertSeverity:
            event = make_alert_event(severity=severity)
            assert event.severity == severity

    def test_requires_human_verification(self):
        event = make_alert_event(requires_human_verification=True)
        assert event.requires_human_verification is True

    def test_metadata_dict(self):
        event = make_alert_event(metadata={"source": "zone_monitor", "count": 3})
        assert event.metadata["source"] == "zone_monitor"

    def test_similarity_score_above_one_fails(self):
        with pytest.raises(ValidationError):
            make_alert_event(similarity_score=1.1)

    def test_similarity_score_below_zero_fails(self):
        with pytest.raises(ValidationError):
            make_alert_event(similarity_score=-0.5)

    def test_json_round_trip(self):
        event = make_alert_event(
            severity=AlertSeverity.CRITICAL,
            similarity_score=0.95,
            requires_human_verification=True,
            metadata={"reason": "high_similarity"},
        )
        restored = AlertEvent.model_validate_json(event.model_dump_json())
        assert event.event_id == restored.event_id
        assert restored.severity == AlertSeverity.CRITICAL
        assert restored.requires_human_verification is True
        assert restored.metadata["reason"] == "high_similarity"


# ─────────────────────────────────────────────────────────────────────────────
# Enum completeness Tests
# ─────────────────────────────────────────────────────────────────────────────

class TestEnums:

    def test_camera_profile_values(self):
        values = {p.value for p in CameraProfile}
        assert "high_security" in values
        assert "balanced" in values
        assert "high_throughput" in values
        assert "crowd_only" in values

    def test_identity_tag_values(self):
        values = {t.value for t in IdentityTag}
        assert "enrolled" in values
        assert "visitor" in values
        assert "unknown" in values

    def test_alert_type_values(self):
        values = {a.value for a in AlertType}
        assert "restricted_entry" in values
        assert "intruder" in values
        assert "headcount_breach" in values
        assert "crowd_density" in values
        assert "pedestrian_anomaly" in values

    def test_alert_severity_values(self):
        values = {s.value for s in AlertSeverity}
        assert "critical" in values
        assert "high" in values
        assert "medium" in values
        assert "low" in values

    def test_event_type_values(self):
        values = {z.value for z in EventType}
        assert "entered" in values
        assert "exited" in values
        assert "dwell" in values

    def test_crowd_model_values(self):
        values = {c.value for c in CrowdModel}
        assert "csrnet" in values
        assert "dmcount" in values
        assert "crowdformer" in values

    def test_density_level_values(self):
        values = {d.value for d in DensityLevel}
        assert "low" in values
        assert "medium" in values
        assert "high" in values
        assert "critical" in values

    def test_operator_role_values(self):
        values = {o.value for o in OperatorRole}
        assert "superadmin" in values
        assert "admin" in values
        assert "operator" in values
        assert "viewer" in values

    def test_zone_type_values(self):
        values = {z.value for z in ZoneType}
        assert "restricted" in values
        assert "monitored" in values
        assert "safe" in values