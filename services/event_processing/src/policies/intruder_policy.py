from collections import Counter
from collections.abc import Set as AbstractSet
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


def _unidentified(zone_type, authorized_person_ids):
    return classify_intruder(
        identity_tag=None,
        person_id=None,
        zone_type=zone_type,
        authorized_person_ids=authorized_person_ids,
        is_blocklisted=False,
        similarity_score=0.0,
    )


def decide(
    rows: list[dict],
    zone_type: str,
    authorized_person_ids: list[str],
    blocklisted_person_ids: set[str],
    conflicted_person_ids: AbstractSet[str] = frozenset(),
) -> tuple[IntruderClassification | None, dict | None]:
    """
    Classify one track from ALL its recognition rows in the time window.

    Returns (classification, chosen_row). classification is None when no
    alert is needed. Order:
      1. a blocklisted person in any row -> blocklisted (every zone)
      2. non-restricted zone -> no alert
      3. no rows at all -> fail closed (unidentified_in_restricted)
      4. an ENROLLED person of this track that is also matched on ANOTHER
         track of the same camera in the window (`conflicted_person_ids`)
         -> the identity is unverified: fail closed
      5. authorized only if ONE authorized enrolled person holds a strict
         majority of the track's rows; authorized rows without a majority
         -> fail closed (a single stray match never authorizes)
      6. otherwise the highest-similarity row decides
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

    if zone_type != ZoneType.RESTRICTED.value:
        return None, None

    if not rows:
        return _unidentified(zone_type, authorized_person_ids), None

    enrolled = [
        r for r in rows
        if r["identity_tag"] == IdentityTag.ENROLLED.value and r.get("person_id")
    ]
    if any(r["person_id"] in conflicted_person_ids for r in enrolled):
        return _unidentified(zone_type, authorized_person_ids), None

    authorized_rows = [r for r in enrolled if r["person_id"] in authorized_person_ids]
    if authorized_rows:
        person_id, votes = Counter(r["person_id"] for r in authorized_rows).most_common(1)[0]
        if 2 * votes > len(rows):
            chosen = max(
                (r for r in authorized_rows if r["person_id"] == person_id),
                key=lambda r: float(r["similarity_score"]),
            )
            return None, chosen
        return _unidentified(zone_type, authorized_person_ids), None

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
