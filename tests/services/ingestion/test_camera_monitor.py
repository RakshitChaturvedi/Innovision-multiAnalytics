import pytest

from services.ingestion.src.camera_monitor import CameraMonitor

def test_monitor_is_online_after_frame(mocker):
    mock_time = mocker.patch("services.ingestion.src.camera_monitor.time.monotonic")
    monitor = CameraMonitor(offline_timeout_seconds=10)

    mock_time.return_value = 100.0
    monitor.frame_received()

    mock_time.return_value = 105.0
    assert monitor.is_online()

def test_monitor_is_offline_after_timeout(mocker):
    mock_time = mocker.patch("services.ingestion.src.camera_monitor.time.monotonic")
    monitor = CameraMonitor(offline_timeout_seconds=10)

    mock_time.return_value = 100.0
    monitor.frame_received()

    mock_time.return_value = 111.0
    assert monitor.is_offline()


def test_seconds_since_last_frame(mocker):
    mock_time = mocker.patch("services.ingestion.src.camera_monitor.time.monotonic")
    monitor = CameraMonitor(offline_timeout_seconds=10)

    mock_time.return_value = 100.0
    monitor.frame_received()

    mock_time.return_value = 103.5
    assert monitor.seconds_since_last_frame() == pytest.approx(3.5)