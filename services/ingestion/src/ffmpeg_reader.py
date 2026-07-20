from __future__ import annotations

import subprocess
from pathlib import Path

import numpy as np

class FFmpegReader:
    # read raw bgr frames from ffmpeg
    def __init__(self, video_path: Path, width: int, height: int) -> None:
        self._video_path = Path(video_path)
        self._width = width
        self._height = height
        self._frame_size = width*height*3
        self._process: subprocess.Popen | None = None

    def start(self) -> None:
        # launch ffmpeg subprocess
        self._process = subprocess.Popen(
            [
                "ffmpeg",
                "-loglevel",
                "error",
                "-i",
                str(self._video_path),
                "-f",
                "rawvideo",
                "-pix_fmt",
                "bgr24",
                "pipe:1",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )
    
    def read_frame(self) -> np.ndarray | None:
        if self._process is None or self._process.stdout is None:
            raise RuntimeError("FFmpegReader has not been started.")
        
        raw = self._process.stdout.read(self._frame_size)
        if len(raw) != self._frame_size:
            return None
        
        return np.frombuffer(raw, dtype=np.uint8).reshape(
            (self._height, self._width, 3)
        )
    
    def stop(self) -> None:
        if self._process is None:
            return
        
        self._process.terminate()
        self._process.wait()

        self._process = None