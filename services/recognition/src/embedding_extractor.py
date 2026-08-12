"""
Extracts the unit-normalized 512-d ArcFace embedding from a Face object
that RecognitionModelLoader.detect_best_face() already produced.

No model call happens here. app.get() runs SCRFD detection and ArcFace
recognition together in a single pass, so the embedding is already
sitting on the Face object by the time quality_gate has finished
evaluating it — this just normalizes what's already there.
"""
import logging

import numpy as np
from insightface.app.common import Face

logger = logging.getLogger(__name__)


def extract_from_face(face: Face) -> np.ndarray | None:
    if face.embedding is None:
        return None

    embedding = face.embedding.astype(np.float32)
    norm = np.linalg.norm(embedding)
    if norm == 0:
        logger.warning("embedding_zero_norm")
        return None

    return embedding / norm  # unit vector, so dot product == cosine similarity
