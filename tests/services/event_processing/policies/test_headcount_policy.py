from services.event_processing.src.policies.headcount_policy import assess_headcount


def test_no_threshold_returns_none():
    result = assess_headcount("zone-1", "Zone A", 5, None)
    assert result is None


def test_under_threshold_no_breach():
    result = assess_headcount("zone-1", "Zone A", 5, 10)
    assert result is not None
    assert result.is_breach is False


def test_at_threshold_no_breach():
    result = assess_headcount("zone-1", "Zone A", 10, 10)
    assert result is not None
    assert result.is_breach is False


def test_over_threshold_is_breach():
    result = assess_headcount("zone-1", "Zone A", 11, 10)
    assert result is not None
    assert result.is_breach is True
    assert result.threshold == 10
    assert result.count == 11
    assert result.zone_id == "zone-1"
