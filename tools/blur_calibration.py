"""Synthetic face-like frames for calibrating the recognition blur gate.

Used by tools/calibrate_blur.py (prints the score table) and by
tests/services/recognition/test_blur_calibration.py (asserts on it), so the
numbers in docs/DEMO_TUNING.md, the default threshold and the tests all come
from the same images.

No real faces: a skin ellipse with eyes, brows, nose, mouth, hair strands and
pore-scale texture, drawn on a 4K "master" frame with a fixed seed. Each case
is scaled the way a camera + platform ingestion would, JPEG q85 round-tripped,
and the face box is cropped out exactly like FaceCropper does.
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

import cv2
import numpy as np

MASTER_W, MASTER_H = 3840, 2160
# Face box in normalized frame coordinates (about 1/5 of the frame height).
FACE_BOX = (0.45, 0.30, 0.5625, 0.50)  # x1, y1, x2, y2
SCALES = {"640x360": (640, 360), "1280x720": (1280, 720), "1920x1080": (1920, 1080)}
JPEG_QUALITY = 85
BLUR_SIGMA = 3.0
LOW_DETAIL_PX = 60  # real detail of a low-res phone camera face
UPSCALED_PX = 400  # what the platform scaler turns it into


@lru_cache(maxsize=4)
def _master(seed: int = 7) -> np.ndarray:
    """Cached; callers must not modify the returned frame."""
    rng = np.random.default_rng(seed)
    img = np.full((MASTER_H, MASTER_W, 3), (95, 105, 110), np.uint8)
    # Mild background texture so the crop margin is not flat.
    img = cv2.add(img, rng.integers(0, 12, img.shape, dtype=np.uint8))

    x1, y1, x2, y2 = FACE_BOX
    fx1, fy1 = int(x1 * MASTER_W), int(y1 * MASTER_H)
    fx2, fy2 = int(x2 * MASTER_W), int(y2 * MASTER_H)
    w, h = fx2 - fx1, fy2 - fy1
    cx, cy = (fx1 + fx2) // 2, (fy1 + fy2) // 2

    face = np.zeros((MASTER_H, MASTER_W), np.uint8)
    cv2.ellipse(face, (cx, cy + h // 20), (int(w * 0.40), int(h * 0.46)), 0, 0, 360, 255, -1)
    img[face > 0] = (120, 150, 200)  # BGR skin
    # Pore-scale texture on the skin only (generated for the face box only).
    region = (slice(fy1, fy2 + h // 10), slice(fx1, fx2))
    skin = face[region] > 0
    pores = rng.normal(0, 9, skin.shape + (3,)).astype(np.float32)
    textured = np.clip(img[region].astype(np.float32) + pores, 0, 255).astype(np.uint8)
    img[region][skin] = textured[skin]

    # Hair: dark cap plus individual strands.
    cv2.ellipse(img, (cx, cy - h // 4), (int(w * 0.44), int(h * 0.26)), 0, 180, 360, (30, 35, 45), -1)
    for _ in range(400):
        sx = int(rng.integers(cx - w * 0.44, cx + w * 0.44))
        sy = int(rng.integers(cy - h // 2, cy - h // 6))
        cv2.line(img, (sx, sy), (sx + int(rng.integers(-15, 15)), sy + int(rng.integers(10, 50))),
                 (int(rng.integers(10, 70)),) * 3, 2)

    ex = int(w * 0.17)
    ey = cy - h // 12
    for side in (-1, 1):
        ecx = cx + side * ex
        cv2.line(img, (ecx - w // 10, ey - h // 11), (ecx + w // 10, ey - h // 10), (40, 45, 60), 12)  # brow
        cv2.ellipse(img, (ecx, ey), (w // 11, h // 30), 0, 0, 360, (245, 245, 245), -1)  # sclera
        cv2.circle(img, (ecx, ey), h // 36, (60, 40, 30), -1)  # iris
        cv2.circle(img, (ecx, ey), h // 90, (5, 5, 5), -1)  # pupil
        cv2.ellipse(img, (ecx, ey), (w // 11, h // 30), 0, 180, 360, (40, 40, 50), 4)  # lid
    cv2.line(img, (cx, ey + h // 30), (cx - w // 30, cy + h // 8), (90, 115, 160), 6)  # nose ridge
    cv2.ellipse(img, (cx, cy + h // 8), (w // 18, h // 50), 0, 0, 180, (70, 90, 130), 5)  # nostrils
    cv2.ellipse(img, (cx, cy + h // 4), (w // 7, h // 30), 0, 0, 180, (60, 60, 150), 8)  # mouth
    return img


def _jpeg(img: np.ndarray) -> np.ndarray:
    ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, JPEG_QUALITY])
    assert ok
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


def _face_crop(frame: np.ndarray) -> np.ndarray:
    h, w = frame.shape[:2]
    x1, y1, x2, y2 = FACE_BOX
    return frame[int(y1 * h):int(y2 * h), int(x1 * w):int(x2 * w)]


@dataclass
class Case:
    name: str
    crop: np.ndarray
    expect_pass: bool | None  # None: no fixed expectation (reported only)


def platform_cases(seed: int = 7) -> list[Case]:
    """Each source resolution as the platform delivers it: JPEG from the
    camera, then scaled to 1920x1080 by ingestion and JPEG'd again."""
    master = _master(seed)
    out: list[Case] = []
    for sigma in (0.0, BLUR_SIGMA):
        for label, size in SCALES.items():
            frame = cv2.resize(master, size, interpolation=cv2.INTER_AREA)
            if sigma:
                frame = cv2.GaussianBlur(frame, (0, 0), sigma)
            frame = cv2.resize(_jpeg(frame), SCALES["1920x1080"], interpolation=cv2.INTER_LINEAR)
            name = f"{'sharp' if not sigma else f'sigma{sigma:g}'} {label} -> platform 1920x1080"
            out.append(Case(name, _face_crop(_jpeg(frame)), not sigma))
    return out


def cases(seed: int = 7) -> list[Case]:
    master = _master(seed)
    out: list[Case] = []
    for label, size in SCALES.items():
        frame = cv2.resize(master, size, interpolation=cv2.INTER_AREA)
        out.append(Case(f"sharp {label}", _face_crop(_jpeg(frame)), True))
    for label, size in SCALES.items():
        frame = cv2.resize(master, size, interpolation=cv2.INTER_AREA)
        frame = cv2.GaussianBlur(frame, (0, 0), BLUR_SIGMA)
        out.append(Case(f"sigma{BLUR_SIGMA:g} {label}", _face_crop(_jpeg(frame)), False))
    # Low-res phone camera: ~60x60 px of real face detail, JPEG'd by the
    # phone, then upscaled by the platform to ~400x400 and JPEG'd again.
    face = _face_crop(master)
    small = _jpeg(cv2.resize(face, (LOW_DETAIL_PX, LOW_DETAIL_PX), interpolation=cv2.INTER_AREA))
    up = cv2.resize(small, (UPSCALED_PX, UPSCALED_PX), interpolation=cv2.INTER_LINEAR)
    out.append(Case(f"low-detail {LOW_DETAIL_PX}px -> {UPSCALED_PX}px", _jpeg(up), None))
    return out
