"""Pure unit tests (no I/O): quality gate pose order, sampler restart, vector parsing."""
import numpy as np

from services.recognition.src.embedding_cache import parse_vector
from services.recognition.src.quality_gate import QualityGate
from services.recognition.src.sampling import RecognitionSampler

from .conftest import FakeFace

gate = QualityGate()


def evaluate(pose):
    return gate.evaluate_face(FakeFace(pose=pose), blur_score=500.0, face_size_px=100,
                              pose_yaw_max=45.0, pose_pitch_max=30.0)


def test_pose_order_is_pitch_yaw_roll():
    # pose = [pitch, yaw, roll]
    r = evaluate((0.0, 60.0, 0.0))  # yaw 60 > 45
    assert not r.passes and r.rejection_reason.startswith("extreme_yaw")
    r = evaluate((40.0, 0.0, 0.0))  # pitch 40 > 30
    assert not r.passes and r.rejection_reason.startswith("extreme_pitch")


def test_pose_within_limits_passes_and_yaw_uses_index_1():
    # Old code read yaw=pose[0]: 40 <= 45 would pass yaw but pitch=pose[1]=40 > 30 rejected.
    # Correct: pitch 10 (ok), yaw 40 (ok, <=45) -> passes.
    assert evaluate((10.0, 40.0, 5.0)).passes
    assert evaluate((-10.0, -40.0, 90.0)).passes  # roll is not gated, sign ignored


def test_sampler_restart_resets_and_samples():
    s = RecognitionSampler(sample_rate=10)
    assert s.should_sample("cam", 1, frame_seq=100, quality_score=0.5)   # new track
    assert not s.should_sample("cam", 1, frame_seq=101, quality_score=0.5)
    assert s.should_sample("cam", 1, frame_seq=5, quality_score=0.5)     # sequence went back
    assert not s.should_sample("cam", 1, frame_seq=6, quality_score=0.5)
    assert s.should_sample("cam", 1, frame_seq=15, quality_score=0.5)    # rate counted from the reset


def test_sampler_without_reset_would_have_waited():
    s = RecognitionSampler(sample_rate=10)
    s.should_sample("cam", 1, frame_seq=100000, quality_score=0.5)
    assert s.should_sample("cam", 1, frame_seq=3, quality_score=0.5)


def test_parse_vector_variants():
    a = parse_vector("[0.5,-1.0,2]")
    assert a.dtype == np.float32 and a.tolist() == [0.5, -1.0, 2.0]
    assert parse_vector(b"[1,2]").dtype == np.float32
    assert parse_vector([1, 2]).dtype == np.float32
    assert parse_vector(np.array([1.0, 2.0])).dtype == np.float32
    assert parse_vector(None) is None and parse_vector("[]") is None
