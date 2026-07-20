from pathlib import Path
import numpy as np
import pytest

from services.ingestion.src.ffmpeg_reader import FFmpegReader

@pytest.fixture
def process_mock(mocker):
    process = mocker.Mock()
    process.stdout.read.return_value = (b"\x00" * (640 * 480 * 3))

    mocker.patch(
        "services.ingestion.src.ffmpeg_reader.subprocess.Popen",
        return_value=process,
    )
    return process

@pytest.fixture
def reader():
    return FFmpegReader(
        video_path=Path("video.mp4"),
        width=640,
        height=480,
    )

def test_start_launches_ffmpeg(
    reader,
    process_mock,
):
    reader.start()
    assert reader._process is process_mock

def test_read_frame_returns_numpy_array(
    reader,
    process_mock,
):
    reader.start()

    frame = reader.read_frame()

    assert isinstance(frame, np.ndarray)
    assert frame.shape == (480, 640, 3)

def test_read_frame_returns_none_on_eof(
    reader,
    process_mock,
):
    reader.start()
    process_mock.stdout.read.return_value = b""

    assert reader.read_frame() is None

def test_stop_terminates_process(
    reader,
    process_mock,
):
    reader.start()
    reader.stop()

    process_mock.terminate.assert_called_once()
    process_mock.wait.assert_called_once()