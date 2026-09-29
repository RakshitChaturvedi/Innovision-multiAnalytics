"""Every settings class exposes redis_url as a METHOD (one API). Detection had
a @property while main.py called it -> TypeError at startup. Recognition and
event_processing use shared.config.Settings."""
import pytest

from services.detection.src.config import DetectionSettings
from shared.config import Settings


@pytest.mark.parametrize("cls", [Settings, DetectionSettings])
def test_redis_url_is_a_method_returning_a_redis_url(cls):
    assert not isinstance(getattr(cls, "redis_url"), property)
    assert callable(getattr(cls, "redis_url"))
    url = cls().redis_url()
    assert url.startswith("redis://") and url.rsplit("/", 1)[1].isdigit()
