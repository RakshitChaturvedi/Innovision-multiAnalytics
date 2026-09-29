from services.event_processing.src.policies.intruder_policy import (
    alert_type_for,
    classify_intruder,
    decide,
    severity_for,
)
from shared.schemas.enums import AlertSeverity


def row(tag, person=None, sim=0.9):
    return {"identity_tag": tag, "person_id": person, "similarity_score": sim}


def test_alert_type_mapping():
    for reason in ("blocklisted", "unknown_in_restricted", "unidentified_in_restricted"):
        assert alert_type_for(reason) == "intruder"
    for reason in ("enrolled_unauthorized", "visitor_in_restricted"):
        assert alert_type_for(reason) == "restricted_entry"


def test_severities():
    assert severity_for("blocklisted") == AlertSeverity.CRITICAL
    assert severity_for("unidentified_in_restricted") == AlertSeverity.HIGH


def test_no_identity_in_restricted_fails_closed():
    c = classify_intruder(None, None, "restricted", [], False, 0.0)
    assert (c.reason, c.severity) == ("unidentified_in_restricted", AlertSeverity.HIGH)


def test_no_identity_outside_restricted_is_fine():
    assert classify_intruder(None, None, "monitored", [], False, 0.0) is None


def test_blocklisted_still_beats_unidentified():
    c = classify_intruder(None, None, "restricted", [], True, 0.0)
    assert c.reason == "blocklisted"


def test_decide_no_rows_restricted_and_monitored():
    assert decide([], "restricted", [], set())[0].reason == "unidentified_in_restricted"
    assert decide([], "monitored", [], set())[0] is None


def test_decide_single_authorized_row_among_others_does_not_authorize():
    """Old rule: ANY authorized row authorized the track (one swapped match
    was enough). Now it needs a strict majority; otherwise fail closed."""
    rows = [row("unknown", sim=0.99), row("enrolled", "p1", 0.5)]
    c, chosen = decide(rows, "restricted", ["p1"], set())
    assert c.reason == "unidentified_in_restricted" and chosen is None


def test_decide_authorized_strict_majority_authorizes():
    rows = [row("enrolled", "p1", 0.8)] * 5 + [row("visitor", "p1", 0.62)]
    c, chosen = decide(rows, "restricted", ["p1"], set())
    assert c is None and chosen["person_id"] == "p1"


def test_decide_exactly_half_is_not_a_majority():
    rows = [row("enrolled", "p1", 0.8)] * 2 + [row("unknown", sim=0.3)] * 2
    assert decide(rows, "restricted", ["p1"], set())[0].reason == "unidentified_in_restricted"


def test_decide_majority_must_be_one_person():
    rows = [row("enrolled", "p1"), row("enrolled", "p2"), row("unknown", sim=0.2)]
    c, _ = decide(rows, "restricted", ["p1", "p2"], set())
    assert c.reason == "unidentified_in_restricted"


def test_decide_cross_track_conflict_fails_closed():
    rows = [row("enrolled", "p1", 0.9)] * 3
    c, chosen = decide(rows, "restricted", ["p1"], set(), conflicted_person_ids={"p1"})
    assert c.reason == "unidentified_in_restricted" and chosen is None


def test_decide_conflict_does_not_hide_blocklist_and_ignores_monitored():
    rows = [row("enrolled", "p1", 0.9)]
    c, _ = decide(rows, "restricted", ["p1"], {"p1"}, conflicted_person_ids={"p1"})
    assert c.reason == "blocklisted"
    assert decide(rows, "monitored", [], set(), conflicted_person_ids={"p1"})[0] is None


def test_decide_uses_highest_similarity_otherwise():
    rows = [row("visitor", sim=0.4), row("unknown", sim=0.8)]
    c, chosen = decide(rows, "restricted", [], set())
    assert c.reason == "unknown_in_restricted" and chosen["similarity_score"] == 0.8


def test_decide_blocklisted_on_lower_similarity_row_still_wins():
    rows = [row("unknown", sim=0.99), row("enrolled", "p2", 0.3)]
    c, chosen = decide(rows, "monitored", [], {"p2"})
    assert c.reason == "blocklisted" and chosen["person_id"] == "p2"
