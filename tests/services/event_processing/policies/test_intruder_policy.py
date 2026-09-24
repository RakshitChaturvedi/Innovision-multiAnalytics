from shared.schemas.enums import (
    AlertSeverity,
    IdentityTag,
)

from services.event_processing.src.policies.intruder_policy import (
    classify_intruder,
)


RESTRICTED = "restricted"
MONITORED = "monitored"


AUTHORIZED = [
    "person-1",
    "person-2",
]


def test_blocklisted_person_always_intruder():

    result = classify_intruder(
        identity_tag=IdentityTag.ENROLLED,
        person_id="person-1",
        zone_type=MONITORED,
        authorized_person_ids=AUTHORIZED,
        is_blocklisted=True,
        similarity_score=0.95,
    )

    assert result is not None
    assert result.reason == "blocklisted"
    assert result.severity == AlertSeverity.CRITICAL


def test_unknown_in_restricted_is_intruder():

    result = classify_intruder(
        identity_tag=IdentityTag.UNKNOWN,
        person_id=None,
        zone_type=RESTRICTED,
        authorized_person_ids=AUTHORIZED,
        is_blocklisted=False,
        similarity_score=0.0,
    )

    assert result is not None
    assert result.reason == "unknown_in_restricted"
    assert result.severity == AlertSeverity.CRITICAL


def test_authorized_enrolled_person_is_not_intruder():

    result = classify_intruder(
        identity_tag=IdentityTag.ENROLLED,
        person_id="person-1",
        zone_type=RESTRICTED,
        authorized_person_ids=AUTHORIZED,
        is_blocklisted=False,
        similarity_score=0.92,
    )

    assert result is None


def test_enrolled_unauthorized_is_intruder():

    result = classify_intruder(
        identity_tag=IdentityTag.ENROLLED,
        person_id="person-99",
        zone_type=RESTRICTED,
        authorized_person_ids=AUTHORIZED,
        is_blocklisted=False,
        similarity_score=0.88,
    )

    assert result is not None
    assert result.reason == "enrolled_unauthorized"
    assert result.severity == AlertSeverity.HIGH


def test_visitor_in_restricted_is_intruder():

    result = classify_intruder(
        identity_tag=IdentityTag.VISITOR,
        person_id="person-99",
        zone_type=RESTRICTED,
        authorized_person_ids=AUTHORIZED,
        is_blocklisted=False,
        similarity_score=0.70,
    )

    assert result is not None
    assert result.reason == "visitor_in_restricted"


def test_unknown_in_monitored_zone_is_not_intruder():

    result = classify_intruder(
        identity_tag=IdentityTag.UNKNOWN,
        person_id=None,
        zone_type=MONITORED,
        authorized_person_ids=[],
        is_blocklisted=False,
        similarity_score=0.0,
    )

    assert result is None