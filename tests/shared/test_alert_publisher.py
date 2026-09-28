"""Platform alert builder + AlertPublisher. Runs on FAKES."""
from datetime import datetime, timezone
from uuid import uuid4

import pytest
from pydantic import ValidationError

from shared.alerting.publisher import AlertPublisher, build_platform_alert
from shared.frames import minio_frame_key
from shared.platform_contracts.alert_event import AlertEvent, AlertEventValidator
from shared.platform_contracts.enums import FrameProvider, SourceUC
from shared.schemas.enums import AlertSeverity as LocalSeverity, AlertType

CAM = uuid4()
TS = datetime(2026, 1, 1, tzinfo=timezone.utc)


def build(**kw):
    args = dict(
        domain_event_id=uuid4(), camera_id=CAM, timestamp=TS,
        severity=LocalSeverity.HIGH, alert_type="intruder",
        title="Intruder", description="someone entered", frame_seq=7,
        metadata={},
    )
    args.update(kw)
    return build_platform_alert(**args)


def test_output_validates_with_platform_model_and_validator():
    a, b = uuid4(), uuid4()
    alert = build(metadata={"source_event_ids": [a, b], "zone_id": "z"})
    assert isinstance(alert, AlertEvent)
    assert AlertEvent.model_validate_json(alert.model_dump_json()) == alert
    assert AlertEventValidator.validate(alert, {CAM}) == []
    assert alert.metadata["source_event_ids"] == [str(a), str(b)]
    assert alert.source_uc == SourceUC.UC1
    assert alert.severity.value == "high"


def test_source_uc_from_env(monkeypatch):
    monkeypatch.setenv("SOURCE_UC", "uc3")
    assert build().source_uc == SourceUC.UC3


def test_alert_id_deterministic_per_domain_event_and_type():
    d = uuid4()
    assert build(domain_event_id=d).alert_id == build(domain_event_id=d).alert_id
    assert build(domain_event_id=d).alert_id != build(domain_event_id=uuid4()).alert_id
    assert build(domain_event_id=d).alert_id != build(
        domain_event_id=d, alert_type="headcount_breach").alert_id
    assert build(domain_event_id=d).source_event_id == d


def test_alert_type_enum_is_coerced_to_string():
    assert build(alert_type=AlertType.INTRUDER).alert_type == "intruder"


@pytest.mark.parametrize("seq", [None, 0, 7])
def test_frame_reference_and_provider_both_or_neither(seq):
    alert = build(frame_seq=seq)
    if seq is None:
        assert alert.frame_reference is None and alert.frame_provider is None
    else:
        assert alert.frame_reference == minio_frame_key(CAM, seq)
        assert alert.frame_provider == FrameProvider.MINIO


def test_platform_model_rejects_mismatched_provider_reference():
    with pytest.raises(ValidationError):
        AlertEvent(camera_id=CAM, timestamp=TS, severity="high", alert_type="x",
                   title="t", description="d", source_event_id=uuid4(),
                   source_uc="uc1", frame_reference="frames/x", frame_provider=None)


def test_description_falls_back_to_title_and_truncation():
    assert build(description=None, title="T").description == "T"
    alert = build(title="t" * 500, description="d" * 5000)
    assert len(alert.title) == 200 and len(alert.description) == 2000


class FakeRedis:
    def __init__(self, fail=None):
        self.fail, self.calls = fail, []

    async def xadd(self, stream, fields, **kw):
        if self.fail:
            raise self.fail
        self.calls.append((stream, fields, kw))
        return "1-0"


async def test_publish_xadds_validated_json():
    redis = FakeRedis()
    alert = build()
    await AlertPublisher(redis).publish(alert)
    (stream, fields, kw), = redis.calls
    assert stream == "alerts:live"
    assert AlertEvent.model_validate_json(fields["data"]) == alert
    assert kw == {"maxlen": 10000, "approximate": True}


@pytest.mark.parametrize("field", ["title", "description"])
async def test_publish_rejects_whitespace(field):
    redis = FakeRedis()
    with pytest.raises(ValueError):
        await AlertPublisher(redis).publish(build(**{field: "   "}))
    assert redis.calls == []


async def test_publish_raises_on_redis_failure():
    with pytest.raises(ConnectionError):
        await AlertPublisher(FakeRedis(fail=ConnectionError("down"))).publish(build())
