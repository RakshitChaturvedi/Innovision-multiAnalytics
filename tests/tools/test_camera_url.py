"""tools/platform/camera_url.py with real ffmpeg against a local DroidCam-like MJPEG server."""
import shutil
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import cv2
import numpy as np
import pytest

from tests.tools import fakes
from tools.platform import camera_url
from tools.platform.common import Reporter

pytestmark = pytest.mark.skipif(shutil.which("ffmpeg") is None, reason="ffmpeg not installed")


def _mjpeg_server(w, h):
    ok, buf = cv2.imencode(".jpg", np.full((h, w, 3), 128, np.uint8))
    jpg = buf.tobytes()

    class H(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path != "/video":
                self.send_error(404)
                return
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            try:
                for _ in range(50):
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n"
                                     + f"Content-Length: {len(jpg)}\r\n\r\n".encode() + jpg + b"\r\n")
                    time.sleep(0.05)
            except (BrokenPipeError, ConnectionResetError):
                pass

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


@pytest.fixture
def mjpeg():
    servers = []

    def make(w, h):
        s = _mjpeg_server(w, h)
        servers.append(s)
        return fakes.userinfo_url("http", f"127.0.0.1:{s.server_address[1]}", "/video",
                                  user="admin", label="mjpeg")

    yield make
    for s in servers:
        s.shutdown()
        s.server_close()


def test_droidcam_mjpeg_16_9_passes_and_hides_credentials(mjpeg, tmp_path, capsys):
    url = mjpeg(640, 360)
    r = Reporter()
    camera_url.check(url, tmp_path / "f.jpg", r)
    out = capsys.readouterr().out
    assert r.failed == 0 and r.warned == 0, out
    assert "640x360" in out and "PASS aspect" in out
    assert fakes.MARKER not in out and "admin:" not in out
    assert cv2.imread(str(tmp_path / "f.jpg")).shape[:2] == (360, 640)


def test_4_3_source_warns(mjpeg, tmp_path, capsys):
    r = Reporter()
    camera_url.check(mjpeg(640, 480), tmp_path / "f.jpg", r)
    out = capsys.readouterr().out
    assert r.failed == 0 and r.warned == 1
    assert "WARN aspect" in out and "1920x1080" in out


def test_unreachable_url_fails_masked(tmp_path, capsys):
    r = Reporter()
    camera_url.check(fakes.userinfo_url("rtsp", "127.0.0.1:1", "/stream", label="rtsp"), tmp_path / "f.jpg", r)
    out = capsys.readouterr().out
    assert r.failed == 1 and "FIX:" in out
    assert fakes.MARKER not in out and "user:" not in out


def test_main_exit_codes(mjpeg, tmp_path):
    assert camera_url.main(["--url", mjpeg(1280, 720), "--out", str(tmp_path / "a.jpg")]) == 0
    assert camera_url.main(["--url", "http://127.0.0.1:1/video", "--out", str(tmp_path / "b.jpg")]) == 1
