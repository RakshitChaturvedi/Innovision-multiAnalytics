"""Camera discovery. FAKES only: a stub registry callable, no HTTP, no Redis."""
import logging
import uuid
from unittest.mock import patch

import pytest

from services.detection.src import camera_discovery as cd
from services.detection.src.config import settings
from services.detection.src.consumer import DetectionConsumer

A, B, C = (str(uuid.uuid4()) for _ in range(3))
LOGGER = "services.detection.src"


@pytest.fixture
def consumer_factory(monkeypatch):
    def make(registry, static="", url="http://registry"):
        monkeypatch.setattr(settings, "detection_camera_ids", static)
        monkeypatch.setattr(settings, "camera_registry_url", url)
        monkeypatch.setattr(settings, "source_uc", "uc1")
        with patch("services.detection.src.consumer.create_async_engine"):
            return DetectionConsumer(registry_get_json=registry)

    return make


class Registry:
    def __init__(self, body=None, error=None):
        self.body, self.error, self.calls = body, error, []

    def __call__(self, url, timeout):
        self.calls.append((url, timeout))
        if self.error:
            raise self.error
        return self.body


def ok(*ids):
    return {"uc_id": "uc1", "camera_ids": list(ids)}


async def test_registry_url_shape_and_timeout():
    reg = Registry(ok(A))
    ids = await cd.fetch_registry_camera_ids("http://reg:8000/", "uc1", 5.0, get_json=reg)
    assert ids == [A]
    assert reg.calls == [("http://reg:8000/cameras/by-uc/uc1", 5.0)]


async def test_invalid_uuids_skipped_with_warning(caplog):
    caplog.set_level(logging.WARNING)
    ids = await cd.fetch_registry_camera_ids(
        "http://r", "uc1", 5, get_json=Registry(ok(A, "not-a-uuid", 12, B.upper()))
    )
    assert ids == [A, B]
    assert "camera_id_invalid" in caplog.text


@pytest.mark.parametrize("body", [
    ["a-list"], {"uc_id": "uc1"}, {"camera_ids": "nope"}, {"cameras": [A]}, None,
])
async def test_other_shapes_rejected_not_guessed(body):
    with pytest.raises(cd.RegistryShapeError):
        await cd.fetch_registry_camera_ids("http://r", "uc1", 5, get_json=Registry(body))


async def test_network_failure_is_unavailable():
    with pytest.raises(cd.RegistryUnavailable):
        await cd.fetch_registry_camera_ids(
            "http://r", "uc1", 5, get_json=Registry(error=TimeoutError("slow"))
        )


async def test_streams_follow_registry_add_and_remove(consumer_factory):
    reg = Registry(ok(A, B))
    c = consumer_factory(reg)
    await c._sync_cameras()
    assert c.streams == [f"frames:{A}", f"frames:{B}"]

    c._tracker_manager.get(B)  # B has tracker state
    reg.body = ok(A, C)
    await c._sync_cameras()
    assert sorted(c.streams) == sorted([f"frames:{A}", f"frames:{C}"])
    assert B not in c._tracker_manager.camera_ids()


async def test_registry_down_keeps_list_and_warns(consumer_factory, caplog):
    reg = Registry(ok(A))
    c = consumer_factory(reg)
    await c._sync_cameras()
    caplog.set_level(logging.WARNING)
    reg.error = ConnectionError("refused")
    await c._sync_cameras()
    assert c.streams == [f"frames:{A}"]
    assert c.stats()["registry_errors"] == 1
    assert any(r.levelno == logging.WARNING and "camera_registry_unavailable" in r.message
               for r in caplog.records)


async def test_bad_response_logs_error_and_keeps_list(consumer_factory, caplog):
    reg = Registry(ok(A))
    c = consumer_factory(reg)
    await c._sync_cameras()
    caplog.set_level(logging.WARNING)
    reg.body = {"cameras": [B]}
    await c._sync_cameras()
    assert c.streams == [f"frames:{A}"]
    assert any(r.levelno == logging.ERROR and "camera_registry_bad_response" in r.message
               for r in caplog.records)


async def test_empty_registry_list_removes_all_cameras(consumer_factory):
    reg = Registry(ok(A))
    c = consumer_factory(reg)
    await c._sync_cameras()
    reg.body = ok()
    await c._sync_cameras()
    assert c.streams == []


async def test_static_ids_win_and_registry_is_never_called(consumer_factory):
    reg = Registry(ok(C))
    c = consumer_factory(reg, static=f"{A}, {B} ,bogus")
    await c._sync_cameras()
    assert c.streams == [f"frames:{A}", f"frames:{B}"]
    assert reg.calls == []
