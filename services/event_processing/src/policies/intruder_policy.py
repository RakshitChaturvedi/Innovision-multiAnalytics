from dataclasses import dataclass

from shared.schemas.enums import (
    AlertSeverity,
    IdentityTag,
    ZoneType,
)


@dataclass(frozen=True)
class IntruderClassification:
    reason: str
    severity: AlertSeverity


def classify_intruder(
    identity_tag: IdentityTag,
    person_id: str | None,
    zone_type: str,
    authorized_person_ids: list[str],
    is_blocklisted: bool,
    similarity_score: float,
) -> IntruderClassification | None:
    """
    Determine whether a zone entry should be classified as an intruder.

    Returns:
        IntruderClassification when the person is unauthorized.
        None when the person is allowed or the zone has no access restriction.

    Rules:
        1. Blocklisted person -> intruder regardless of zone.
        2. Non-restricted zone -> no intruder.
        3. Authorized enrolled person in restricted zone -> no intruder.
        4. Enrolled person not authorized -> intruder.
        5. Unknown person in restricted zone -> intruder.
        6. Visitor in restricted zone without authorization -> intruder.
    """

    # Rule 1:
    # Blocklist has highest priority and applies in every zone.
    if is_blocklisted:
        return IntruderClassification(
            reason="blocklisted",
            severity=AlertSeverity.CRITICAL,
        )

    # Rules below only apply to restricted zones.
    if zone_type != ZoneType.RESTRICTED.value:
        return None

    # Known enrolled person who is explicitly authorized.
    if (
        identity_tag == IdentityTag.ENROLLED
        and person_id is not None
        and person_id in authorized_person_ids
    ):
        return None

    # Known enrolled person who is not authorized.
    if identity_tag == IdentityTag.ENROLLED:
        return IntruderClassification(
            reason="enrolled_unauthorized",
            severity=AlertSeverity.HIGH,
        )

    # Unknown person in restricted zone.
    if identity_tag == IdentityTag.UNKNOWN:
        return IntruderClassification(
            reason="unknown_in_restricted",
            severity=AlertSeverity.CRITICAL,
        )

    # Visitor in restricted zone.
    if identity_tag == IdentityTag.VISITOR:
        return IntruderClassification(
            reason="visitor_in_restricted",
            severity=AlertSeverity.HIGH,
        )

    return IntruderClassification(
        reason="unauthorized",
        severity=AlertSeverity.HIGH,
    )