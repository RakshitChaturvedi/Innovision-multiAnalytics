from datetime import datetime, timezone
from uuid import UUID, uuid4

import pytest
from pydantic import ValidationError

from shared.schemas import (
    AlertEvent,
    AlertSeverity,
    AlertStatus,
    AlertType,
    BoundingBox,
    CameraProfile,
    CrowdFrameEvent,
    CrowdModel,
    DensityLevel,
    DetectionEvent,
    EventType,
    FrameEvent,
    FrameProvider,
    IdentityTag,
    OperatorRole,
    RecognitionEvent,
    TrackResult,
    ZoneEvent,
    ZoneType,
)


# =============================================================================
# Helpers
# =============================================================================

def utcnow():
    return datetime.now(timezone.utc)


def make_bbox(**overrides):
    defaults = {
        "x1": 0.10,
        "y1": 0.10,
        "x2": 0.50,
        "y2": 0.50,
    }
    defaults.update(overrides)
    return BoundingBox(**defaults)


def make_track(**overrides):
    defaults = {
        "track_id": 1,
        "bbox": make_bbox(),
        "confidence": 0.92,
        "class_label": "person",
        "has_face": False,
        "face_bbox": None,
    }
    defaults.update(overrides)
    return TrackResult(**defaults)


def make_frame(**overrides):
    defaults = {
        "camera_id": uuid4(),
        "timestamp": utcnow(),
        "frame_seq": 1,
        "frame_reference": "frame-000001",
        "frame_provider": FrameProvider.REDIS,
        "frame_shape": (1920, 1080),
        "profile": CameraProfile.BALANCED,
    }
    defaults.update(overrides)
    return FrameEvent(**defaults)


def make_detection(**overrides):
    defaults = {
        "camera_id": uuid4(),
        "frame_event_id": uuid4(),
        "timestamp": utcnow(),
        "frame_reference": "frame-000001",
        "frame_seq": 1,
        "frame_shape": (1920, 1080),
        "tracks": [],
        "profile": CameraProfile.BALANCED,
        "inference_latency_ms": 12.4,
    }
    defaults.update(overrides)
    return DetectionEvent(**defaults)


def make_recognition(**overrides):
    defaults = {
        "camera_id": uuid4(),
        "detection_event_id": uuid4(),
        "track_id": 1,
        "timestamp": utcnow(),
        "frame_reference": "frame-000001",
        "frame_seq": 1,
        "identity_tag": IdentityTag.ENROLLED,
        "similarity_score": 0.91,
        "quality_score": 0.88,
    }
    defaults.update(overrides)
    return RecognitionEvent(**defaults)


def make_zone(**overrides):
    defaults = {
        "camera_id": uuid4(),
        "zone_id": uuid4(),
        "frame_seq": 1,
        "track_id": 1,
        "timestamp": utcnow(),
        "event_type": EventType.ENTERED,
    }
    defaults.update(overrides)
    return ZoneEvent(**defaults)


def make_crowd(**overrides):
    defaults = {
        "camera_id": uuid4(),
        "frame_reference": "frame-000001",
        "frame_shape": (1920, 1080),
        "frame_seq": 1,
        "timestamp": utcnow(),
        "crowd_model": CrowdModel.CSRNET,
        "zone_ids": [uuid4()],
    }
    defaults.update(overrides)
    return CrowdFrameEvent(**defaults)


def make_alert(**overrides):
    defaults = {
        "camera_id": uuid4(),
        "timestamp": utcnow(),
        "severity": AlertSeverity.HIGH,
        "alert_type": AlertType.RESTRICTED_ENTRY,
        "source_event_ids": [uuid4()],
    }
    defaults.update(overrides)
    return AlertEvent(**defaults)


# =============================================================================
# BoundingBox Tests
# =============================================================================

class TestBoundingBox:

    def test_valid_box(self):
        box = make_bbox()

        assert box.x1 == pytest.approx(0.1)
        assert box.y1 == pytest.approx(0.1)
        assert box.x2 == pytest.approx(0.5)
        assert box.y2 == pytest.approx(0.5)

    def test_centroid(self):
        box = make_bbox()

        assert box.centroid == pytest.approx((0.3, 0.3))

    def test_area(self):
        box = make_bbox()

        assert box.area == pytest.approx(0.16)

    def test_coordinates_below_zero_fail(self):
        with pytest.raises(ValidationError):
            make_bbox(x1=-0.1)

    def test_coordinates_above_one_fail(self):
        with pytest.raises(ValidationError):
            make_bbox(x2=1.2)

    def test_edge_values_allowed(self):
        box = make_bbox(
            x1=0,
            y1=0,
            x2=1,
            y2=1,
        )

        assert box.area == pytest.approx(1.0)

    def test_no_extra_fields(self):
        box = make_bbox()

        assert not hasattr(box, "confidence")


# =============================================================================
# TrackResult Tests
# =============================================================================

class TestTrackResult:

    def test_valid_track(self):
        track = make_track()

        assert track.track_id == 1
        assert track.class_label == "person"
        assert track.has_face is False
        assert track.face_bbox is None

    def test_confidence(self):
        track = make_track(confidence=0.75)

        assert track.confidence == pytest.approx(0.75)

    def test_confidence_above_one_fails(self):
        with pytest.raises(ValidationError):
            make_track(confidence=1.5)

    def test_confidence_below_zero_fails(self):
        with pytest.raises(ValidationError):
            make_track(confidence=-0.2)

    def test_face_bbox(self):
        face = make_bbox(
            x1=0.2,
            y1=0.2,
            x2=0.4,
            y2=0.4,
        )

        track = make_track(
            has_face=True,
            face_bbox=face,
        )

        assert track.has_face is True
        assert isinstance(track.face_bbox, BoundingBox)

    def test_bbox_is_bounding_box(self):
        track = make_track()

        assert isinstance(track.bbox, BoundingBox)

    def test_missing_track_id_fails(self):
        with pytest.raises(ValidationError):
            TrackResult(
                bbox=make_bbox(),
                confidence=0.8,
                class_label="person",
            )

    def test_missing_confidence_fails(self):
        with pytest.raises(ValidationError):
            TrackResult(
                track_id=1,
                bbox=make_bbox(),
                class_label="person",
            )

# =============================================================================
# FrameEvent Tests
# =============================================================================

class TestFrameEvent:

    def test_valid_frame_event(self):
        event = make_frame()

        assert isinstance(event.event_id, UUID)
        assert isinstance(event.camera_id, UUID)
        assert event.frame_seq == 1
        assert event.frame_reference == "frame-000001"
        assert event.frame_provider == FrameProvider.REDIS
        assert event.frame_shape == (1920, 1080)
        assert event.profile == CameraProfile.BALANCED

    def test_frame_seq_validation(self):
        with pytest.raises(ValidationError):
            make_frame(frame_seq=-1)

    def test_different_frame_provider(self):
        event = make_frame(
            frame_provider=FrameProvider.SHARED_MEMORY
        )

        assert event.frame_provider == FrameProvider.SHARED_MEMORY

    def test_camera_profile(self):
        event = make_frame(
            profile=CameraProfile.HIGH_SECURITY
        )

        assert event.profile == CameraProfile.HIGH_SECURITY

    def test_frame_shape(self):
        event = make_frame(
            frame_shape=(1280, 720)
        )

        assert event.frame_shape == (1280, 720)

    def test_json_roundtrip(self):
        event = make_frame()

        serialized = event.model_dump_json()
        reconstructed = FrameEvent.model_validate_json(serialized)

        assert reconstructed == event

    def test_missing_camera_id_fails(self):
        with pytest.raises(ValidationError):
            FrameEvent(
                timestamp=utcnow(),
                frame_seq=1,
                frame_reference="frame",
                frame_provider=FrameProvider.REDIS,
                frame_shape=(1920, 1080),
                profile=CameraProfile.BALANCED,
            )

    def test_missing_frame_reference_fails(self):
        with pytest.raises(ValidationError):
            FrameEvent(
                camera_id=uuid4(),
                timestamp=utcnow(),
                frame_seq=1,
                frame_provider=FrameProvider.REDIS,
                frame_shape=(1920, 1080),
                profile=CameraProfile.BALANCED,
            )

    def test_missing_frame_provider_fails(self):
        with pytest.raises(ValidationError):
            FrameEvent(
                camera_id=uuid4(),
                timestamp=utcnow(),
                frame_seq=1,
                frame_reference="frame",
                frame_shape=(1920, 1080),
                profile=CameraProfile.BALANCED,
            )


# =============================================================================
# DetectionEvent Tests
# =============================================================================

class TestDetectionEvent:

    def test_valid_detection_event(self):
        tracks = [
            make_track(),
            make_track(track_id=2),
        ]

        event = make_detection(
            tracks=tracks
        )

        assert isinstance(event.event_id, UUID)
        assert isinstance(event.frame_event_id, UUID)

        assert event.frame_reference == "frame-000001"
        assert event.frame_seq == 1

        assert len(event.tracks) == 2
        assert event.tracks[0].track_id == 1
        assert event.tracks[1].track_id == 2

        assert event.profile == CameraProfile.BALANCED
        assert event.inference_latency_ms == pytest.approx(12.4)

    def test_empty_tracks(self):
        event = make_detection(
            tracks=[]
        )

        assert event.tracks == []

    def test_multiple_tracks(self):
        tracks = [
            make_track(track_id=i)
            for i in range(10)
        ]

        event = make_detection(
            tracks=tracks
        )

        assert len(event.tracks) == 10

    def test_profile_change(self):
        event = make_detection(
            profile=CameraProfile.HIGH_THROUGHPUT
        )

        assert event.profile == CameraProfile.HIGH_THROUGHPUT

    def test_frame_shape(self):
        event = make_detection(
            frame_shape=(3840, 2160)
        )

        assert event.frame_shape == (3840, 2160)

    def test_latency(self):
        event = make_detection(
            inference_latency_ms=25.7
        )

        assert event.inference_latency_ms == pytest.approx(25.7)

    def test_json_roundtrip(self):
        event = make_detection(
            tracks=[
                make_track(),
                make_track(track_id=2),
            ]
        )

        serialized = event.model_dump_json()
        reconstructed = DetectionEvent.model_validate_json(serialized)

        assert reconstructed == event

    def test_missing_tracks_fails(self):
        with pytest.raises(ValidationError):
            DetectionEvent(
                camera_id=uuid4(),
                frame_event_id=uuid4(),
                timestamp=utcnow(),
                frame_reference="frame",
                frame_seq=1,
                frame_shape=(1920, 1080),
                profile=CameraProfile.BALANCED,
                inference_latency_ms=10,
            )

    def test_missing_frame_reference_fails(self):
        with pytest.raises(ValidationError):
            DetectionEvent(
                camera_id=uuid4(),
                frame_event_id=uuid4(),
                timestamp=utcnow(),
                frame_seq=1,
                frame_shape=(1920, 1080),
                tracks=[],
                profile=CameraProfile.BALANCED,
                inference_latency_ms=10,
            )

    def test_missing_frame_event_id_fails(self):
        with pytest.raises(ValidationError):
            DetectionEvent(
                camera_id=uuid4(),
                timestamp=utcnow(),
                frame_reference="frame",
                frame_seq=1,
                frame_shape=(1920, 1080),
                tracks=[],
                profile=CameraProfile.BALANCED,
                inference_latency_ms=10,
            )
# =============================================================================
# RecognitionEvent Tests
# =============================================================================

class TestRecognitionEvent:

    def test_valid_recognition_event(self):
        event = make_recognition()

        assert isinstance(event.event_id, UUID)
        assert isinstance(event.detection_event_id, UUID)

        assert event.track_id == 1
        assert event.frame_reference == "frame-000001"
        assert event.frame_seq == 1

        assert event.identity_tag == IdentityTag.ENROLLED
        assert event.similarity_score == pytest.approx(0.91)
        assert event.quality_score == pytest.approx(0.88)

        assert event.person_id is None
        assert event.embedding_id is None
        assert event.liveness_score is None
        assert event.liveness_checked is False

    def test_unknown_identity(self):
        event = make_recognition(
            identity_tag=IdentityTag.UNKNOWN,
            similarity_score=0.0,
        )

        assert event.identity_tag == IdentityTag.UNKNOWN
        assert event.similarity_score == 0.0

    def test_person_and_embedding(self):
        person_id = uuid4()
        embedding_id = uuid4()

        event = make_recognition(
            person_id=person_id,
            embedding_id=embedding_id,
        )

        assert event.person_id == person_id
        assert event.embedding_id == embedding_id

    def test_liveness(self):
        event = make_recognition(
            liveness_checked=True,
            liveness_score=0.97,
        )

        assert event.liveness_checked is True
        assert event.liveness_score == pytest.approx(0.97)

    def test_similarity_lower_bound(self):
        event = make_recognition(
            similarity_score=0.0,
        )

        assert event.similarity_score == 0.0

    def test_similarity_upper_bound(self):
        event = make_recognition(
            similarity_score=1.0,
        )

        assert event.similarity_score == 1.0

    def test_similarity_above_one_fails(self):
        with pytest.raises(ValidationError):
            make_recognition(
                similarity_score=1.1,
            )

    def test_similarity_below_zero_fails(self):
        with pytest.raises(ValidationError):
            make_recognition(
                similarity_score=-0.1,
            )

    def test_quality_above_one_fails(self):
        with pytest.raises(ValidationError):
            make_recognition(
                quality_score=1.2,
            )

    def test_quality_below_zero_fails(self):
        with pytest.raises(ValidationError):
            make_recognition(
                quality_score=-0.3,
            )

    def test_json_roundtrip(self):
        event = make_recognition(
            liveness_checked=True,
            liveness_score=0.95,
            person_id=uuid4(),
            embedding_id=uuid4(),
        )

        serialized = event.model_dump_json()
        reconstructed = RecognitionEvent.model_validate_json(serialized)

        assert reconstructed == event

    def test_missing_detection_event_id_fails(self):
        with pytest.raises(ValidationError):
            RecognitionEvent(
                camera_id=uuid4(),
                track_id=1,
                timestamp=utcnow(),
                frame_reference="frame",
                frame_seq=1,
                identity_tag=IdentityTag.ENROLLED,
                similarity_score=0.9,
                quality_score=0.8,
            )

    def test_missing_identity_tag_fails(self):
        with pytest.raises(ValidationError):
            RecognitionEvent(
                camera_id=uuid4(),
                detection_event_id=uuid4(),
                track_id=1,
                timestamp=utcnow(),
                frame_reference="frame",
                frame_seq=1,
                similarity_score=0.9,
                quality_score=0.8,
            )


# =============================================================================
# ZoneEvent Tests
# =============================================================================

class TestZoneEvent:

    def test_valid_zone_event(self):
        event = make_zone()

        assert isinstance(event.event_id, UUID)
        assert isinstance(event.zone_id, UUID)

        assert event.frame_seq == 1
        assert event.track_id == 1
        assert event.event_type == EventType.ENTERED

        assert event.person_id is None
        assert event.global_id is None
        assert event.dwell_duration_seconds is None

    def test_entered_event(self):
        event = make_zone(
            event_type=EventType.ENTERED,
        )

        assert event.event_type == EventType.ENTERED

    def test_exited_event(self):
        event = make_zone(
            event_type=EventType.EXITED,
        )

        assert event.event_type == EventType.EXITED

    def test_dwell_event(self):
        event = make_zone(
            event_type=EventType.DWELL,
            dwell_duration_seconds=18.6,
        )

        assert event.event_type == EventType.DWELL
        assert event.dwell_duration_seconds == pytest.approx(18.6)

    def test_person_information(self):
        person = uuid4()
        global_id = uuid4()

        event = make_zone(
            person_id=person,
            global_id=global_id,
        )

        assert event.person_id == person
        assert event.global_id == global_id

    def test_json_roundtrip(self):
        event = make_zone(
            person_id=uuid4(),
            global_id=uuid4(),
            dwell_duration_seconds=42.5,
        )

        serialized = event.model_dump_json()
        reconstructed = ZoneEvent.model_validate_json(serialized)

        assert reconstructed == event

    def test_missing_zone_id_fails(self):
        with pytest.raises(ValidationError):
            ZoneEvent(
                camera_id=uuid4(),
                frame_seq=1,
                track_id=1,
                timestamp=utcnow(),
                event_type=EventType.ENTERED,
            )

    def test_missing_event_type_fails(self):
        with pytest.raises(ValidationError):
            ZoneEvent(
                camera_id=uuid4(),
                zone_id=uuid4(),
                frame_seq=1,
                track_id=1,
                timestamp=utcnow(),
            )

    def test_missing_track_id_fails(self):
        with pytest.raises(ValidationError):
            ZoneEvent(
                camera_id=uuid4(),
                zone_id=uuid4(),
                frame_seq=1,
                timestamp=utcnow(),
                event_type=EventType.ENTERED,
            )
# =============================================================================
# CrowdFrameEvent Tests
# =============================================================================

class TestCrowdFrameEvent:

    def test_valid_crowd_event(self):
        zone_ids = [uuid4(), uuid4()]

        event = make_crowd(
            zone_ids=zone_ids,
        )

        assert isinstance(event.event_id, UUID)
        assert isinstance(event.camera_id, UUID)

        assert event.frame_reference == "frame-000001"
        assert event.frame_seq == 1
        assert event.frame_shape == (1920, 1080)

        assert event.crowd_model == CrowdModel.CSRNET
        assert event.zone_ids == zone_ids

    def test_empty_zone_list(self):
        event = make_crowd(
            zone_ids=[],
        )

        assert event.zone_ids == []

    def test_different_model(self):
        event = make_crowd(
            crowd_model=CrowdModel.DMCOUNT,
        )

        assert event.crowd_model == CrowdModel.DMCOUNT

    def test_crowdformer(self):
        event = make_crowd(
            crowd_model=CrowdModel.CROWDFORMER,
        )

        assert event.crowd_model == CrowdModel.CROWDFORMER

    def test_json_roundtrip(self):
        event = make_crowd()

        serialized = event.model_dump_json()
        reconstructed = CrowdFrameEvent.model_validate_json(serialized)

        assert reconstructed == event

    def test_missing_frame_reference_fails(self):
        with pytest.raises(ValidationError):
            CrowdFrameEvent(
                camera_id=uuid4(),
                frame_shape=(1920, 1080),
                frame_seq=1,
                timestamp=utcnow(),
                crowd_model=CrowdModel.CSRNET,
            )

    def test_missing_model_fails(self):
        with pytest.raises(ValidationError):
            CrowdFrameEvent(
                camera_id=uuid4(),
                frame_reference="frame",
                frame_shape=(1920, 1080),
                frame_seq=1,
                timestamp=utcnow(),
            )


# =============================================================================
# AlertEvent Tests
# =============================================================================

class TestAlertEvent:

    def test_valid_alert(self):
        source_events = [uuid4(), uuid4()]

        event = make_alert(
            source_event_ids=source_events,
        )

        assert isinstance(event.alert_id, UUID)
        assert isinstance(event.camera_id, UUID)

        assert event.severity == AlertSeverity.HIGH
        assert event.alert_type == AlertType.RESTRICTED_ENTRY

        assert event.status == AlertStatus.PENDING
        assert event.source_event_ids == source_events

        assert event.title == "Alert"
        assert event.description is None
        assert event.frame_reference is None
        assert event.metadata == {}

    def test_custom_title(self):
        event = make_alert(
            title="Restricted Entry",
        )

        assert event.title == "Restricted Entry"

    def test_description(self):
        event = make_alert(
            description="Unauthorized person detected.",
        )

        assert event.description == "Unauthorized person detected."

    def test_frame_reference(self):
        event = make_alert(
            frame_reference="frame-123",
        )

        assert event.frame_reference == "frame-123"

    def test_metadata(self):
        metadata = {
            "zone": "Server Room",
            "confidence": 0.98,
        }

        event = make_alert(
            metadata=metadata,
        )

        assert event.metadata == metadata

    def test_status(self):
        event = make_alert(
            status=AlertStatus.RESOLVED,
        )

        assert event.status == AlertStatus.RESOLVED

    @pytest.mark.parametrize(
        "severity",
        [
            AlertSeverity.CRITICAL,
            AlertSeverity.HIGH,
            AlertSeverity.MEDIUM,
            AlertSeverity.LOW,
        ],
    )
    def test_all_severities(self, severity):
        event = make_alert(
            severity=severity,
        )

        assert event.severity == severity

    @pytest.mark.parametrize(
        "alert_type",
        [
            AlertType.RESTRICTED_ENTRY,
            AlertType.INTRUDER,
            AlertType.HEADCOUNT_BREACH,
            AlertType.CROWD_DENSITY,
        ],
    )
    def test_all_alert_types(self, alert_type):
        event = make_alert(
            alert_type=alert_type,
        )

        assert event.alert_type == alert_type

    def test_json_roundtrip(self):
        event = make_alert(
            frame_reference="frame-42",
            metadata={"zone": "A"},
            status=AlertStatus.ACKNOWLEDGED,
        )

        serialized = event.model_dump_json()
        reconstructed = AlertEvent.model_validate_json(serialized)

        assert reconstructed == event

    def test_missing_source_events_fails(self):
        with pytest.raises(ValidationError):
            AlertEvent(
                camera_id=uuid4(),
                timestamp=utcnow(),
                severity=AlertSeverity.HIGH,
                alert_type=AlertType.INTRUDER,
            )


# =============================================================================
# Enum Tests
# =============================================================================

class TestEnums:

    def test_camera_profiles(self):
        assert CameraProfile.HIGH_SECURITY.value == "high_security"
        assert CameraProfile.BALANCED.value == "balanced"
        assert CameraProfile.HIGH_THROUGHPUT.value == "high_throughput"
        assert CameraProfile.CROWD_ONLY.value == "crowd_only"

    def test_identity_tags(self):
        assert IdentityTag.ENROLLED.value == "enrolled"
        assert IdentityTag.VISITOR.value == "visitor"
        assert IdentityTag.UNKNOWN.value == "unknown"

    def test_frame_provider(self):
        assert FrameProvider.REDIS.value == "redis"
        assert FrameProvider.SHARED_MEMORY.value == "shared_memory"

    def test_event_types(self):
        assert EventType.ENTERED.value == "entered"
        assert EventType.EXITED.value == "exited"
        assert EventType.DWELL.value == "dwell"

    def test_alert_status(self):
        assert AlertStatus.PENDING.value == "pending"
        assert AlertStatus.ACKNOWLEDGED.value == "acknowledged"
        assert AlertStatus.RESOLVED.value == "resolved"

    def test_crowd_models(self):
        assert CrowdModel.CSRNET.value == "csrnet"
        assert CrowdModel.DMCOUNT.value == "dmcount"
        assert CrowdModel.CROWDFORMER.value == "crowdformer"

    def test_zone_types(self):
        assert ZoneType.RESTRICTED.value == "restricted"
        assert ZoneType.MONITORED.value == "monitored"
        assert ZoneType.SAFE.value == "safe"

    def test_density_levels(self):
        assert DensityLevel.LOW.value == "low"
        assert DensityLevel.MEDIUM.value == "medium"
        assert DensityLevel.HIGH.value == "high"
        assert DensityLevel.CRITICAL.value == "critical"

    def test_operator_roles(self):
        assert OperatorRole.SUPERADMIN.value == "superadmin"
        assert OperatorRole.ADMIN.value == "admin"
        assert OperatorRole.OPERATOR.value == "operator"
        assert OperatorRole.VIEWER.value == "viewer"