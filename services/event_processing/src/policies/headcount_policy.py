from dataclasses import dataclass


@dataclass(frozen=True)
class HeadcountAssessment:
    zone_id: str
    zone_name: str
    count: int
    threshold: int
    is_breach: bool


def assess_headcount(
    zone_id: str,
    zone_name: str,
    count: int,
    max_headcount: int | None,
) -> HeadcountAssessment | None:
    """
    Returns HeadcountAssessment if the zone has a headcount threshold configured.
    Returns None if no threshold is configured (max_headcount is None).
    """
    if max_headcount is None:
        return None

    return HeadcountAssessment(
        zone_id=zone_id,
        zone_name=zone_name,
        count=count,
        threshold=max_headcount,
        is_breach=(count > max_headcount),
    )
