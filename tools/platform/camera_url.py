"""Grab ONE frame from a camera URL with ffmpeg before registering it.

    python -m tools.platform.camera_url --url http://192.168.1.20:4747/video [--out cam.jpg]
    (scripts\\test_camera_url.ps1 -Url <url>)

Works for DroidCam (HTTP MJPEG) and RTSP. Prints the resolution, fps when the
stream reports it, and WARNs when the aspect ratio is not 16:9 (the platform
stretches every source to 1920x1080). The URL is only ever printed masked.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import time
from pathlib import Path

from tools.platform.common import Reporter, mask_text, mask_url

TIMEOUT_S = 20
ASPECT_TOLERANCE = 0.01


def _clean(text: str, url: str) -> str:
    """ffmpeg echoes the URL in its errors: mask it, keep the last lines only."""
    text = text.replace(url, mask_url(url))
    lines = [ln for ln in mask_text(text).splitlines() if ln.strip()]
    return " | ".join(lines[-3:])[:400]


def _input_args(url: str, fmt: str | None) -> list[str]:
    args = []
    if url.lower().startswith("rtsp"):
        args += ["-rtsp_transport", "tcp"]
    if fmt:
        args += ["-f", fmt]
    return args + ["-i", url]


def grab_frame(url: str, out: Path, timeout: float = TIMEOUT_S) -> str | None:
    """Write one JPG to `out`. None on success, else the (masked) error."""
    attempts = [None]
    if url.lower().startswith("http"):
        attempts.append("mpjpeg")  # multipart MJPEG (DroidCam) when probing fails
    error = "no attempt made"
    for fmt in attempts:
        cmd = ["ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
               *_input_args(url, fmt), "-frames:v", "1", "-q:v", "2", str(out)]
        try:
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except FileNotFoundError:
            return "ffmpeg not found on PATH"
        except subprocess.TimeoutExpired:
            error = f"no frame within {timeout:.0f}s"
            continue
        if p.returncode == 0 and out.exists() and out.stat().st_size > 0:
            return None
        error = _clean(p.stderr or f"ffmpeg exit {p.returncode}", url)
    return error


def probe_fps(url: str, timeout: float = TIMEOUT_S) -> float | None:
    cmd = ["ffprobe", "-v", "error", *(_input_args(url, None)[:-2]), "-select_streams", "v:0",
           "-show_entries", "stream=avg_frame_rate,r_frame_rate", "-of", "json", url]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        streams = json.loads(p.stdout or "{}").get("streams") or []
    except (FileNotFoundError, subprocess.TimeoutExpired, ValueError):
        return None
    for s in streams:
        for key in ("avg_frame_rate", "r_frame_rate"):
            num, _, den = str(s.get(key, "0/0")).partition("/")
            try:
                fps = float(num) / float(den or 1)
            except (ValueError, ZeroDivisionError):
                continue
            if 0 < fps < 240:  # MJPEG streams often report 90000 or 0: unknown
                return round(fps, 2)
    return None


def check(url: str, out: Path, r: Reporter) -> None:
    import cv2

    shown = mask_url(url)
    err = grab_frame(url, out)
    if err:
        r.fail("frame", f"{shown}: {err}",
               "check the phone/camera is on the same Wi-Fi, the app is open with the screen on, "
               "and the URL is the one the app shows (DroidCam: http://<phone-ip>:4747/video)")
        return
    img = cv2.imread(str(out))
    if img is None:
        r.fail("frame", f"{shown}: ffmpeg wrote an unreadable image", "retry; if it persists use RTSP")
        return
    h, w = img.shape[:2]
    r.ok("frame", f"{shown} -> {out} ({w}x{h})")
    fps = probe_fps(url)
    r.ok("fps", f"{fps:g}" if fps else "not reported by the stream (fine for MJPEG)")
    ratio = w / h
    if abs(ratio - 16 / 9) / (16 / 9) <= ASPECT_TOLERANCE:
        r.ok("aspect", f"{w}x{h} is 16:9")
    else:
        r.warn("aspect", f"{w}x{h} is {ratio:.3f}:1, not 16:9 (1.778); the platform stretches it "
               "to 1920x1080, which distorts people and zones",
               "set the camera/app to a 16:9 resolution (1280x720 or 1920x1080)")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--url", required=True)
    ap.add_argument("--out", help="JPG to write (default camera_test_<timestamp>.jpg)")
    args = ap.parse_args(argv)
    out = Path(args.out or f"camera_test_{time.strftime('%Y%m%d_%H%M%S')}.jpg")
    r = Reporter()
    check(args.url, out, r)
    return r.summary()


if __name__ == "__main__":
    raise SystemExit(main())
