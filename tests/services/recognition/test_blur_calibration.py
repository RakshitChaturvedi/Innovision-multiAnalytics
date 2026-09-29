"""Blur gate is resolution-independent (no services: pure OpenCV on synthetic frames).

Same synthetic face (tools/blur_calibration.py) as a 640x360, 1280x720 and
1920x1080 source, JPEG q85 round trip, cropped like FaceCropper does.
Before the fix the score was the raw Laplacian variance of the crop, so it
depended on how many pixels the face had: a sharp 640x360 source upscaled to
1920x1080 by the platform scored 69.5 against a threshold of 100.

The behaviour tests only use QualityGate.precheck_size_blur with the
configured threshold, the API the code had before the fix.
"""
import cv2
import numpy as np
import pytest

from services.recognition.src.config import config
from services.recognition.src.quality_gate import QualityGate
from tools.blur_calibration import cases, platform_cases

CASES = {c.name: c for c in cases()}
SHARP = [c for c in CASES.values() if c.name.startswith("sharp")]
BLURRED = [c for c in CASES.values() if c.name.startswith("sigma3")]
LOW_DETAIL = next(c for c in CASES.values() if c.name.startswith("low-detail"))
PLATFORM = platform_cases()


def _score(crop) -> float:
    return QualityGate().precheck_size_blur(crop, min_face_size_px=10, blur_threshold=0.0).blur_score


def _gate(crop):
    return QualityGate().precheck_size_blur(
        crop, min_face_size_px=10, blur_threshold=config.DEFAULT_BLUR_THRESHOLD,
    )


def test_calibration_set_has_all_cases():
    assert [c.crop.shape[0] for c in SHARP] == [72, 144, 216]
    assert len(BLURRED) == 3
    assert {c.crop.shape[0] for c in PLATFORM} == {216}


def test_same_face_scores_within_25_percent_across_resolutions():
    scores = [_score(c.crop) for c in SHARP]
    assert max(scores) <= 1.25 * min(scores), scores


@pytest.mark.parametrize("case", SHARP, ids=lambda c: c.name)
def test_sharp_face_passes_default_threshold_at_every_scale(case):
    result = _gate(case.crop)
    assert result.passes, (case.name, result.blur_score)


@pytest.mark.parametrize("case", BLURRED, ids=lambda c: c.name)
def test_sigma3_blurred_face_rejected_at_every_scale(case):
    result = _gate(case.crop)
    assert not result.passes, (case.name, result.blur_score)
    assert result.rejection_reason.startswith("too_blurry")


@pytest.mark.parametrize("case", PLATFORM, ids=lambda c: c.name)
def test_platform_upscaled_to_1080p_sharp_passes_blurred_rejected(case):
    """The production failure: every source reaches us as 1920x1080."""
    result = _gate(case.crop)
    assert result.passes is case.expect_pass, (case.name, result.blur_score)


def test_upscaled_low_detail_face_is_between_blurred_and_sharp():
    """~60x60 px of real detail upscaled to 400x400 by the platform: accepted
    at the default threshold, but it scores clearly below a sharp face, so a
    stricter threshold would reject it first."""
    low = _score(LOW_DETAIL.crop)
    assert low >= config.DEFAULT_BLUR_THRESHOLD
    assert low < min(_score(c.crop) for c in SHARP)
    assert low > max(_score(c.crop) for c in BLURRED)


def test_upscaling_alone_does_not_change_the_score_much():
    """The same crop upscaled 1.5x (720p -> 1080p) keeps its score within 25%."""
    crop = CASES["sharp 1280x720"].crop
    up = cv2.resize(crop, None, fx=1.5, fy=1.5, interpolation=cv2.INTER_LINEAR)
    a, b = _score(crop), _score(up)
    assert abs(a - b) <= 0.25 * max(a, b), (a, b)


def test_blur_eval_size_is_configurable():
    from services.recognition.src.quality_gate import blur_score

    crop = CASES["sharp 1920x1080"].crop
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    assert blur_score(gray) == pytest.approx(blur_score(crop))
    assert config.BLUR_EVAL_SIZE == 112
    assert blur_score(crop, 64) != pytest.approx(blur_score(crop, 112))
    via_gate = QualityGate().precheck_size_blur(crop, blur_threshold=0.0, blur_eval_size=64)
    assert via_gate.blur_score == pytest.approx(blur_score(crop, 64))


def test_flat_crop_scores_zero_and_is_rejected():
    flat = np.full((200, 200, 3), 128, np.uint8)
    result = _gate(flat)
    assert result.blur_score == pytest.approx(0.0)
    assert not result.passes


def test_rejections_carry_reason_codes():
    gate = QualityGate()
    small = gate.precheck_size_blur(np.zeros((20, 20, 3), np.uint8), min_face_size_px=40)
    assert (small.passes, small.reason_code) == (False, "too_small")
    blurry = _gate(np.full((200, 200, 3), 128, np.uint8))
    assert blurry.reason_code == "too_blurry"
