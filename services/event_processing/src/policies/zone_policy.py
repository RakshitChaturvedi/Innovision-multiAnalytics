from dataclasses import dataclass
from datetime import datetime

from shared.schemas.enums import EventType


@dataclass
class ZoneTransition:
    zone_id: str
    event_type: EventType
    dwell_duration_seconds: float | None = None


def point_in_polygon(
    point: tuple[float, float],
    polygon: list[list[float]],
) -> bool:
    """
    Ray-casting point-in-polygon test.

    Point and polygon coordinates are normalized to [0, 1].
    """

    if len(polygon) < 3:
        return False

    x, y = point

    inside = False
    j = len(polygon) - 1

    for i in range(len(polygon)):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]

        if ((yi > y) != (yj > y)) and (
            x < (xj - xi) * (y - yi) / (yj - yi) + xi
        ):
            inside = not inside

        j = i

    return inside

def get_centroid(bbox) -> tuple[float, float]:
    """
    Get the center point of a TrackResult BoundingBox.
    """

    return(
        (bbox.x1 + bbox.x2) / 2,
        (bbox.y1 + bbox.y2) / 2
    )


def detect_transitions(
    previous_zones: set[str],
    current_zones: set[str],
    zones_by_id: dict[str, dict],
    entry_times: dict[str, datetime],
    now: datetime,
) -> list[ZoneTransition]:

    transitions: list[ZoneTransition] = []

    # Person entered a zone
    for zone_id in current_zones - previous_zones:
        
        if zone_id not in zones_by_id:
            continue

        transitions.append(
            ZoneTransition(
                zone_id = zone_id,
                event_type=EventType.ENTERED,
            )
        )
    
    # Person exited a zone
    for zone_id in previous_zones - current_zones:

        if zone_id not in zones_by_id:
            continue

        dwell = None

        entry_time = entry_times.get(zone_id)

        if entry_time:
            dwell = (
                now - entry_time
            ).total_seconds()

        transitions.append(
            ZoneTransition(
                zone_id=zone_id,
                event_type=EventType.EXITED,
                dwell_duration_seconds=dwell,
            )
        )

    # Person is still inside a zone
    for zone_id in current_zones & previous_zones:

        zone = zones_by_id.get(zone_id)

        if not zone:
            continue

        entry_time = entry_times.get(zone_id)

        if not entry_time:
            continue

        dwell_seconds = (
            now - entry_time
        ).total_seconds()

        threshold = zone.get(
            "dwell_threshold_seconds",
            60,
        )

        if dwell_seconds >= threshold:

            transitions.append(
                ZoneTransition(
                    zone_id=zone_id,
                    event_type=EventType.DWELL,
                    dwell_duration_seconds=dwell_seconds,
                )
            )

    return transitions
