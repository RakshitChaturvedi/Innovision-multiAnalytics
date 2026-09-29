from datetime import datetime, timedelta, timezone

import pytest

from services.event_processing.src.policies.headcount_policy import (
    BreachState,
    Phase,
    Transition,
    resolve_margin,
    step,
)

T0 = datetime(2026, 1, 1, tzinfo=timezone.utc)


def run(state, samples, max_headcount=10, enter=5, exit_=10):
    """samples: (seconds_offset, rolling_avg). Returns (state, transitions)."""
    out = []
    for offset, avg in samples:
        state, tr = step(
            state, avg, max_headcount, T0 + timedelta(seconds=offset), enter, exit_
        )
        if tr:
            out.append((offset, tr))
    return state, out


@pytest.mark.parametrize("mx,margin", [(1, 1), (5, 1), (10, 1), (11, 2), (25, 3), (100, 10)])
def test_resolve_margin(mx, margin):
    assert resolve_margin(mx) == margin


def test_no_breach_before_enter_seconds():
    state, out = run(BreachState(), [(t, 12) for t in range(0, 5)])
    assert out == []
    assert state.phase is Phase.NORMAL


def test_breach_after_enter_seconds_exactly_once():
    state, out = run(BreachState(), [(t, 12) for t in range(0, 20)])
    assert out == [(5, Transition.OPEN)]
    assert state.phase is Phase.BREACH


def test_at_threshold_is_not_over():
    _, out = run(BreachState(), [(t, 10.0) for t in range(0, 30)])
    assert out == []


def test_dip_resets_enter_timer():
    samples = [(0, 12), (3, 12), (4, 9), (5, 12), (9, 12)]
    _, out = run(BreachState(), samples)
    assert out == []
    _, out = run(BreachState(), samples + [(10, 12)])
    assert out == [(10, Transition.OPEN)]


def test_oscillation_around_max_bounded_by_one_open():
    # instantaneous 10/11/10/11 -> rolling avg hovers at 10.5, then never
    # gets low enough to resolve, so a second OPEN is impossible.
    state, out = run(BreachState(), [(t, 10.5) for t in range(0, 120)])
    assert [tr for _, tr in out] == [Transition.OPEN]
    assert state.phase is Phase.BREACH


def test_flapping_average_never_opens_or_resolves():
    avgs = [10.4, 9.6] * 60
    _, out = run(BreachState(), [(t, a) for t, a in enumerate(avgs)])
    assert out == []
    breach = BreachState(Phase.BREACH)
    _, out = run(breach, [(t, a) for t, a in enumerate(avgs)])
    assert out == []


def test_resolve_after_exit_seconds():
    breach = BreachState(Phase.BREACH)
    state, out = run(breach, [(t, 8) for t in range(0, 15)])
    assert out == [(10, Transition.RESOLVE)]
    assert state.phase is Phase.NORMAL


def test_not_resolved_in_hysteresis_band():
    # max=10, margin=1 -> need <= 9; 9.5 is inside the band
    _, out = run(BreachState(Phase.BREACH), [(t, 9.5) for t in range(0, 60)])
    assert out == []


def test_rise_resets_exit_timer():
    samples = [(0, 8), (5, 8), (6, 10), (7, 8), (16, 8)]
    _, out = run(BreachState(Phase.BREACH), samples)
    assert out == []
    _, out = run(BreachState(Phase.BREACH), samples + [(17, 8)])
    assert out == [(17, Transition.RESOLVE)]


def test_full_cycle_open_resolve_open():
    samples = (
        [(t, 12) for t in range(0, 6)]
        + [(t, 5) for t in range(6, 20)]
        + [(t, 12) for t in range(20, 30)]
    )
    _, out = run(BreachState(), samples)
    assert [tr for _, tr in out] == [Transition.OPEN, Transition.RESOLVE, Transition.OPEN]
