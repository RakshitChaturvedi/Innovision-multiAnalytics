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


# reason -> platform alert_type
_ALERT_TYPES = {
    "blocklisted": "intruder",
    "unknown_in_restricted": "intruder",
    "unidentified_in_restricted": "intruder",
    "enrolled_unauthorized": "restricted_entry",
    "visitor_in_restricted": "restricted_entry",
}

_SEVERITIES = {
    "blocklisted": AlertSeverity.CRITICAL,
    "unknown_in_restricted": AlertSeverity.CRITICAL,
    "unidentified_in_restricted": AlertSeverity.HIGH,
    "enrolled_unauthorized": AlertSeverity.HIGH,
    "visitor_in_restricted": AlertSeverity.HIGH,
}


def alert_type_for(reason: str) -> str:
    return _ALERT_TYPES.get(reason, "intruder")


def severity_for(reason: str) -> AlertSeverity:
    return _SEVERITIES.get(reason, AlertSeverity.HIGH)


def classify_intruder(
    identity_tag: IdentityTag | None,
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
        7. identity_tag None (no recognition at all) in a restricted zone
           -> intruder, fail-closed ("unidentified_in_restricted", HIGH).
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

    # Nobody could be identified: fail closed.
    if identity_tag is None:
        return IntruderClassification(
            reason="unidentified_in_restricted",
            severity=AlertSeverity.HIGH,
        )

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


def decide(
    rows: list[dict],
    zone_type: str,
    authorized_person_ids: list[str],
    blocklisted_person_ids: set[str],
) -> tuple[IntruderClassification | None, dict | None]:
    """
    Classify one track from ALL its recognition rows in the time window.

    Returns (classification, chosen_row). classification is None when no
    alert is needed. Order: blocklisted (any row) wins; then non-restricted
    zones never alert; then no rows at all -> fail closed; then an
    authorized ENROLLED row anywhere in the window -> authorized; otherwise
    the highest-similarity row decides.
    """

    for row in rows:
        if row.get("person_id") in blocklisted_person_ids:
            return (
                classify_intruder(
                    identity_tag=row["identity_tag"],
                    person_id=row["person_id"],
                    zone_type=zone_type,
                    authorized_person_ids=authorized_person_ids,
                    is_blocklisted=True,
                    similarity_score=float(row["similarity_score"]),
                ),
                row,
            )

    if not rows:
        return (
            classify_intruder(
                identity_tag=None,
                person_id=None,
                zone_type=zone_type,
                authorized_person_ids=authorized_person_ids,
                is_blocklisted=False,
                similarity_score=0.0,
            ),
            None,
        )

    for row in rows:
        if (
            row["identity_tag"] == IdentityTag.ENROLLED.value
            and row.get("person_id") in authorized_person_ids
        ):
            return None, row

    best = max(rows, key=lambda r: float(r["similarity_score"]))

    return (
        classify_intruder(
            identity_tag=IdentityTag(best["identity_tag"]),
            person_id=best.get("person_id"),
            zone_type=zone_type,
            authorized_person_ids=authorized_person_ids,
            is_blocklisted=False,
            similarity_score=float(best["similarity_score"]),
        ),
        best,
    )
