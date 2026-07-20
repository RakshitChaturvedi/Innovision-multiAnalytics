import pytest
import numpy as np

from services.ingestion.src.encoder import JPEGEncoder

@pytest.fixture
def encoder():
    return JPEGEncoder()

@pytest.fixture
def frame():
    return np.zeros((480,640,3), dtype=np.uint8)

def test_encode_returns_bytes(encoder, frame):
    jpeg = encoder.encode(frame)

    assert isinstance(jpeg, bytes)
    assert len(jpeg) > 0

def test_same_frame_produces_same_output(encoder, frame):
    jpeg1 = encoder.encode(frame)
    jpeg2 = encoder.encode(frame)

    assert jpeg1 == jpeg2

def test_invalid_frame_taises(encoder):
    with pytest.raises(Exception):
        encoder.encode(None)