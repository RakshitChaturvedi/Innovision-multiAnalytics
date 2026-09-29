from collections import Counter
from collections.abc import Mapping
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


def resolve_identity_conflicts(
    track_id: int,
    enrolled_counts: Mapping[str, Mapping[int, int]],
) -> tuple[set[str], set[str]]:
    """
    One person cannot be two tracks at once. For every person matched
    (ENROLLED) on several tracks of the same camera in the window, only a
    clearly dominant track keeps the identity: strictly the most rows AND
    at least twice the runner-up.

    `enrolled_counts` is {person_id: {track_id: enrolled rows}}.

    Returns (conflicted, unverified) for `track_id`:
      conflicted -> no track dominates: this track fails closed
      unverified -> another track dominates: this track's rows of that
                    person carry no identity
    """

    conflicted: set[str] = set()
    unverified: set[str] = set()

    for person_id, per_track in enrolled_counts.items():
        tracks = {t: n for t, n in per_track.items() if n > 0}
        if len(tracks) < 2 or track_id not in tracks:
            continue

        (winner, top), (_, runner_up) = sorted(
            tracks.items(), key=lambda kv: kv[1], reverse=True
        )[:2]

        if top > runner_up and top >= 2 * runner_up:
            if winner != track_id:
                unverified.add(person_id)
        else:
            conflicted.add(person_id)

    return conflicted, unverified


_IDENTIFIED = (IdentityTag.ENROLLED.value, IdentityTag.VISITOR.value)


def _identity(row) -> tuple[str, str | None] | None:
    """(tag, person) of a row that asserts an identity; None for UNKNOWN."""
    if row["identity_tag"] not in _IDENTIFIED:
        return None
    return row["identity_tag"], row.get("person_id")


def trim_to_current_identity(rows: list[dict]) -> list[dict]:
    """
    A track whose latest rows assert a different identity than its older
    rows (ByteTrack id switch: enrolled P, then a visitor) keeps only the
    rows since that switch. UNKNOWN rows (face turned away) assert nothing
    and never cause a switch.
    """

    # Stable: rows without a timestamp keep their given order.
    ordered = sorted(rows, key=lambda r: r.get("timestamp") or 0)
    current = None
    for i in range(len(ordered) - 1, -1, -1):
        identity = _identity(ordered[i])
        if identity is None:
            continue
        if current is None:
            current = identity
        elif identity != current:
            return ordered[i + 1:]
    return ordered


def classification_rows(rows: list[dict], authorized_person_ids) -> list[dict]:
    """
    The rows a track is classified from. The switch trimming only ever
    fails closed: if the track's CURRENT identity is an authorized enrolled
    person, older contrary rows are kept (they still count against the
    majority); otherwise only the rows since the switch count.
    """

    trimmed = trim_to_current_identity(rows)
    latest = next(
        (i for i in map(_identity, reversed(trimmed)) if i is not None), None
    )
    if (
        latest is not None
        and latest[0] == IdentityTag.ENROLLED.value
        and latest[1] in authorized_person_ids
    ):
        return rows
    return trimmed


def resolve_ownership(
    track_id: int,
    person_ids,
    recent_rows_by_track: Mapping[int, list[dict]],
) -> tuple[set[str], set[str]]:
    """
    Recency-based version of resolve_identity_conflicts. Evidence is the
    ENROLLED rows of each person per track among the track's RECENT rows
    (caller passes only rows from the last RECOGNITION_RECENT_S before the
    decision time), after switch trimming (trim_to_current_identity), so a
    track that switched to someone else no longer owns its old identity.

    Returns (conflicted, unverified) for `track_id`:
      conflicted -> this track and others hold recent evidence, none
                    dominates: fail closed
      unverified -> another track owns the person (or several do without a
                    winner) and this track has no recent evidence of its own
    A person nobody has recent evidence for is neither.
    """

    counts: dict[str, dict[int, int]] = {}
    for t, rows in recent_rows_by_track.items():
        for r in trim_to_current_identity(rows):
            if r["identity_tag"] == IdentityTag.ENROLLED.value and r.get("person_id"):
                per = counts.setdefault(r["person_id"], {})
                per[t] = per.get(t, 0) + 1

    conflicted: set[str] = set()
    unverified: set[str] = set()
    for person_id in person_ids:
        per_track = counts.get(person_id, {})
        if not per_track:
            continue
        if track_id not in per_track:
            unverified.add(person_id)
            continue
        c, u = resolve_identity_conflicts(track_id, {person_id: per_track})
        conflicted |= c
        unverified |= u
    return conflicted, unverified


def decide(
    rows: list[dict],
    zone_type: str,
    authorized_person_ids: list[str],
    blocklisted_person_ids: set[str],
    conflicted_person_ids: AbstractSet[str] = frozenset(),
    unverified_person_ids: AbstractSet[str] = frozenset(),
) -> tuple[IntruderClassification | None, dict | None]:
    """
    Classify one track from ALL its recognition rows in the time window.

    Returns (classification, chosen_row). classification is None when no
    alert is needed. Order:
      1. a blocklisted person in any row -> blocklisted (every zone)
      2. non-restricted zone -> no alert
      3. no rows at all -> fail closed (unidentified_in_restricted);
         then the rows are narrowed by classification_rows (id switch)
      4. an ENROLLED person of this track that is also matched on another
         track with no clearly dominant track (`conflicted_person_ids`, see
         resolve_identity_conflicts) -> fail closed
      5. rows of a person that another track dominates
         (`unverified_person_ids`) carry no identity: they still count in
         the majority denominator, but never as an identity
      6. authorized only if ONE authorized enrolled person holds a strict
         majority of ALL the track's rows; authorized rows without a
         majority -> fail closed (a single stray match never authorizes)
      7. otherwise the highest-similarity identified row decides; no
         identified row left -> fail closed
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

    rows = classification_rows(rows, authorized_person_ids)

    enrolled = [
        r for r in rows
        if r["identity_tag"] == IdentityTag.ENROLLED.value and r.get("person_id")
    ]
    if any(r["person_id"] in conflicted_person_ids for r in enrolled):
        return _unidentified(zone_type, authorized_person_ids), None

    def lost(r):
        return (
            r["identity_tag"] == IdentityTag.ENROLLED.value
            and r.get("person_id") in unverified_person_ids
        )

    enrolled = [r for r in enrolled if not lost(r)]
    identified = [r for r in rows if not lost(r)]

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

    if not identified:
        return _unidentified(zone_type, authorized_person_ids), None

    best = max(identified, key=lambda r: float(r["similarity_score"]))

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
