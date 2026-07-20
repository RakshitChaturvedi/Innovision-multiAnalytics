import pytest

from services.ingestion.src.sampler import FrameSampler

def test_first_frame_is_kept(mocker):
    mock_time = mocker.patch("services.ingestion.src.sampler.time.monotonic")
    sampler = FrameSampler(target_fps=10)

    mock_time.return_value = 0.0
    assert sampler.should_keep_frame() is True

def test_frame_before_interval_is_dropped(mocker):
    mock_time = mocker.patch("services.ingestion.src.sampler.time.monotonic")
    sampler = FrameSampler(target_fps=10)
    
    mock_time.return_value = 0.0
    assert sampler.should_keep_frame() is True

    mock_time.return_value = 0.05
    assert sampler.should_keep_frame() is False

def test_frame_after_interval_is_kept(mocker):
    mock_time = mocker.patch("services.ingestion.src.sampler.time.monotonic")
    sampler = FrameSampler(target_fps=10)

    mock_time.return_value = 0.0
    sampler.should_keep_frame()

    mock_time.return_value = 0.11
    assert sampler.should_keep_frame() is True

def test_reset(mocker):
    mock_time = mocker.patch("services.ingestion.src.sampler.time.monotonic")
    sampler = FrameSampler(target_fps=10)

    mock_time.return_value = 0.0
    sampler.should_keep_frame()

    sampler.reset()
    mock_time.return_value = 0.0
    assert sampler.should_keep_frame() is True