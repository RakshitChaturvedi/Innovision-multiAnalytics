"""Pure breach state machine for zone headcount (no I/O, event time only)."""
import math
from dataclasses import dataclass
from datetime import datetime
from enum import Enum


class Phase(str, Enum):
    NORMAL = "normal"
    BREACH = "breach"


class Transition(str, Enum):
    OPEN = "open"
    RESOLVE = "resolve"


@dataclass(frozen=True)
class BreachState:
    phase: Phase = Phase.NORMAL
    # Event time at which the condition for leaving `phase` first held
    # continuously. None while the condition does not hold.
    since: datetime | None = None


def resolve_margin(max_headcount: int) -> int:
    return max(1, math.ceil(0.1 * max_headcount))


def step(
    state: BreachState,
    rolling_avg: float,
    max_headcount: int,
    ts: datetime,
    enter_seconds: float,
    exit_seconds: float,
) -> tuple[BreachState, Transition | None]:
    """
    NORMAL -> BREACH: rolling_avg > max continuously for enter_seconds.
    BREACH -> NORMAL: rolling_avg <= max - margin continuously for exit_seconds.
    """
    if state.phase is Phase.NORMAL:
        condition = rolling_avg > max_headcount
        hold, target, transition = enter_seconds, Phase.BREACH, Transition.OPEN
    else:
        condition = rolling_avg <= max_headcount - resolve_margin(max_headcount)
        hold, target, transition = exit_seconds, Phase.NORMAL, Transition.RESOLVE

    if not condition:
        return BreachState(state.phase, None), None

    since = state.since if state.since is not None else ts
    if (ts - since).total_seconds() >= hold:
        return BreachState(target, None), transition
    return BreachState(state.phase, since), None
