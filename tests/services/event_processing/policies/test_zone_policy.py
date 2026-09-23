from datetime import datetime, timedelta, timezone

from shared.schemas.enums import EventType

from services.event_processing.src.policies.zone_policy import (
    detect_transitions,
    point_in_polygon,
)


SQUARE = [
    [0.1, 0.1],
    [0.5, 0.1],
    [0.5, 0.5],
    [0.1, 0.5],
]


def test_point_inside_polygon():
    result = point_in_polygon(
        (0.3, 0.3),
        SQUARE,
    )

    assert result is True


def test_point_outside_polygon():
    result = point_in_polygon(
        (0.7, 0.7),
        SQUARE,
    )

    assert result is False


def test_point_on_boundary_returns_boolean():
    result = point_in_polygon(
        (0.1, 0.1),
        SQUARE,
    )

    assert isinstance(result, bool)


def test_invalid_polygon_returns_false():
    result = point_in_polygon(
        (0.3, 0.3),
        [[0.1, 0.1], [0.5, 0.5]],
    )

    assert result is False


def test_entered_transition():
    now = datetime.now(timezone.utc)

    zones = {
        "zone-1": {
            "name": "Server Room",
            "type": "restricted",
            "dwell_threshold_seconds": 60,
        }
    }

    transitions = detect_transitions(
        previous_zones=set(),
        current_zones={"zone-1"},
        zones_by_id=zones,
        entry_times={},
        now=now,
    )

    assert len(transitions) == 1
    assert transitions[0].zone_id == "zone-1"
    assert transitions[0].event_type == EventType.ENTERED


def test_exited_transition():
    now = datetime.now(timezone.utc)

    zones = {
        "zone-1": {
            "name": "Server Room",
            "type": "restricted",
            "dwell_threshold_seconds": 60,
        }
    }

    entry_time = now - timedelta(seconds=30)

    transitions = detect_transitions(
        previous_zones={"zone-1"},
        current_zones=set(),
        zones_by_id=zones,
        entry_times={
            "zone-1": entry_time,
        },
        now=now,
    )

    assert len(transitions) == 1
    assert transitions[0].zone_id == "zone-1"
    assert transitions[0].event_type == EventType.EXITED
    assert transitions[0].dwell_duration_seconds >= 30


def test_dwell_transition_after_threshold():
    now = datetime.now(timezone.utc)

    zones = {
        "zone-1": {
            "name": "Server Room",
            "type": "restricted",
            "dwell_threshold_seconds": 60,
        }
    }

    entry_time = now - timedelta(seconds=90)

    transitions = detect_transitions(
        previous_zones={"zone-1"},
        current_zones={"zone-1"},
        zones_by_id=zones,
        entry_times={
            "zone-1": entry_time,
        },
        now=now,
    )

    assert len(transitions) == 1
    assert transitions[0].event_type == EventType.DWELL
    assert transitions[0].dwell_duration_seconds >= 90


def test_no_dwell_before_threshold():
    now = datetime.now(timezone.utc)

    zones = {
        "zone-1": {
            "name": "Server Room",
            "type": "restricted",
            "dwell_threshold_seconds": 60,
        }
    }

    entry_time = now - timedelta(seconds=30)

    transitions = detect_transitions(
        previous_zones={"zone-1"},
        current_zones={"zone-1"},
        zones_by_id=zones,
        entry_times={
            "zone-1": entry_time,
        },
        now=now,
    )

    assert transitions == []


def test_unknown_zone_is_ignored():
    now = datetime.now(timezone.utc)

    zones = {}

    transitions = detect_transitions(
        previous_zones=set(),
        current_zones={"unknown-zone"},
        zones_by_id=zones,
        entry_times={},
        now=now,
    )

    assert transitions == []


def test_multiple_zone_entries():
    now = datetime.now(timezone.utc)

    zones = {
        "zone-1": {
            "name": "Restricted",
            "type": "restricted",
            "dwell_threshold_seconds": 60,
        },
        "zone-2": {
            "name": "Monitored",
            "type": "monitored",
            "dwell_threshold_seconds": 60,
        },
    }

    transitions = detect_transitions(
        previous_zones=set(),
        current_zones={"zone-1", "zone-2"},
        zones_by_id=zones,
        entry_times={},
        now=now,
    )

    assert len(transitions) == 2

    event_types = {
        transition.event_type
        for transition in transitions
    }

    assert event_types == {EventType.ENTERED}