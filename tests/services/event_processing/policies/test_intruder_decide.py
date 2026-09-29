from services.event_processing.src.policies.intruder_policy import (
    alert_type_for,
    classify_intruder,
    decide,
    resolve_identity_conflicts,
    resolve_ownership,
    trim_to_current_identity,
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
    # rows are in time order: the visitor row is a blip, not the latest identity
    rows = [row("enrolled", "p1", 0.8)] * 3 + [row("visitor", "p1", 0.62)] + [row("enrolled", "p1", 0.8)] * 2
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


def test_resolve_dominant_track_keeps_identity_others_lose_it():
    counts = {"p": {7: 38, 8: 2}}
    assert resolve_identity_conflicts(7, counts) == (set(), set())
    assert resolve_identity_conflicts(8, counts) == (set(), {"p"})


def test_resolve_exactly_twice_is_enough_and_three_tracks_use_runner_up():
    assert resolve_identity_conflicts(8, {"p": {7: 4, 8: 2}}) == (set(), {"p"})
    counts = {"p": {7: 10, 8: 5, 9: 1}}
    assert resolve_identity_conflicts(7, counts) == (set(), set())
    assert resolve_identity_conflicts(9, counts) == (set(), {"p"})


def test_resolve_tie_or_small_margin_conflicts_every_track():
    for a, b in ((5, 4), (4, 4), (6, 4)):
        counts = {"p": {7: a, 8: b}}
        assert resolve_identity_conflicts(7, counts) == ({"p"}, set())
        assert resolve_identity_conflicts(8, counts) == ({"p"}, set())


def test_resolve_single_track_or_absent_track_is_no_conflict():
    assert resolve_identity_conflicts(7, {"p": {7: 3}}) == (set(), set())
    assert resolve_identity_conflicts(9, {"p": {7: 3, 8: 1}}) == (set(), set())


def test_decide_unverified_rows_only_fail_closed():
    rows = [row("enrolled", "p1", 0.9)] * 2
    c, chosen = decide(rows, "restricted", ["p1"], set(), unverified_person_ids={"p1"})
    assert c.reason == "unidentified_in_restricted" and chosen is None


def test_decide_unverified_rows_still_count_against_majority():
    rows = [row("enrolled", "p1")] * 2 + [row("enrolled", "p2")]
    c, _ = decide(rows, "restricted", ["p1", "p2"], set(), unverified_person_ids={"p1"})
    assert c.reason == "unidentified_in_restricted"


def test_decide_unverified_falls_back_to_other_identified_rows():
    rows = [row("enrolled", "p1", 0.95), row("visitor", sim=0.6)]
    c, chosen = decide(rows, "restricted", [], set(), unverified_person_ids={"p1"})
    assert c.reason == "visitor_in_restricted" and chosen["similarity_score"] == 0.6


def test_decide_blocklist_beats_unverified():
    rows = [row("enrolled", "p1", 0.9)]
    c, _ = decide(rows, "restricted", ["p1"], {"p1"}, unverified_person_ids={"p1"})
    assert c.reason == "blocklisted"


# --- recency / id switch -------------------------------------------------

from datetime import datetime, timedelta, timezone  # noqa: E402

T = datetime(2026, 1, 1, tzinfo=timezone.utc)


def at(s, tag, person=None, sim=0.9):
    return {**row(tag, person, sim), "timestamp": T + timedelta(seconds=s)}


def test_trim_drops_rows_before_an_id_switch():
    rows = [at(0, "enrolled", "p"), at(1, "enrolled", "p"), at(2.6, "visitor", "q")]
    assert trim_to_current_identity(rows) == [rows[2]]


def test_trim_ignores_unknown_rows():
    rows = [at(0, "enrolled", "p"), at(1, "unknown"), at(2, "enrolled", "p")]
    assert trim_to_current_identity(rows) == rows


def test_switch_to_a_visitor_classifies_from_the_new_rows_only():
    rows = [at(t / 10, "enrolled", "p") for t in range(14)] + [at(2.6, "visitor", "q", 0.62)]
    c, _ = decide(rows, "restricted", ["p"], set())
    assert c.reason == "visitor_in_restricted"


def test_trailing_single_visitor_row_is_a_switch_fail_closed():
    rows = [at(i, "enrolled", "p") for i in range(5)] + [at(5, "visitor", "p", 0.62)]
    c, _ = decide(rows, "restricted", ["p"], set())
    assert c.reason == "visitor_in_restricted"


def test_switch_towards_authorized_keeps_older_contrary_rows():
    """Trimming only ever fails closed: 2 rows of p (owned elsewhere) then 1 of
    authorized q is not a q majority."""
    rows = [at(0, "enrolled", "p"), at(1, "enrolled", "p"), at(2, "enrolled", "q")]
    c, _ = decide(rows, "restricted", ["p", "q"], set(), unverified_person_ids={"p"})
    assert c.reason == "unidentified_in_restricted"


def test_ownership_goes_to_recent_evidence_not_old_rows():
    """Track 2 had p at 0-1.3 then switched to a visitor; track 3 has p now."""
    t2 = [at(i / 10, "enrolled", "p") for i in range(14)] + [at(2.6, "visitor", "q")]
    t3 = [at(5.3 + i / 10, "enrolled", "p") for i in range(8)]
    assert resolve_ownership(3, ["p"], {2: t2, 3: t3}) == (set(), set())
    assert resolve_ownership(2, ["p"], {2: t2, 3: t3}) == (set(), {"p"})


def test_track_without_recent_evidence_does_not_own():
    assert resolve_ownership(8, ["p"], {7: [at(0, "enrolled", "p")], 8: []}) == (set(), {"p"})
    assert resolve_ownership(8, ["p"], {}) == (set(), set())


def test_recent_evidence_without_winner_fails_closed():
    rows = {7: [at(0, "enrolled", "p")] * 3, 8: [at(0, "enrolled", "p")] * 2}
    assert resolve_ownership(8, ["p"], rows) == ({"p"}, set())
