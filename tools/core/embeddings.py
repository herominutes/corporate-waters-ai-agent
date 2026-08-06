"""Sentence-transformer embeddings for semantic memory search."""

import logging
import numpy as np

log = logging.getLogger(__name__)

MODEL_NAME = "all-MiniLM-L6-v2"
VECTOR_DIM = 384

_model = None


def _get_model():
    """Lazy-load the sentence-transformer model on first use."""
    global _model
    if _model is None:
        log.info("Loading embedding model %s ...", MODEL_NAME)
        from sentence_transformers import SentenceTransformer
        _model = SentenceTransformer(MODEL_NAME)
        log.info("Embedding model loaded.")
    return _model


def embed(text: str) -> list[float]:
    """Encode a single text into a 384-dim vector."""
    model = _get_model()
    vec = model.encode(text, normalize_embeddings=True)
    return vec.tolist()


def embed_batch(texts: list[str]) -> list[list[float]]:
    """Encode a batch of texts into vectors."""
    if not texts:
        return []
    model = _get_model()
    vecs = model.encode(texts, normalize_embeddings=True, batch_size=64)
    return vecs.tolist()


def cosine_similarity(a: list[float], b: list[float]) -> float:
    """Cosine similarity between two normalized vectors (= dot product)."""
    va = np.asarray(a, dtype=np.float32)
    vb = np.asarray(b, dtype=np.float32)
    return float(np.dot(va, vb))


def vec_to_bytes(vec: list[float]) -> bytes:
    """Serialize a vector to bytes for SQLite BLOB storage."""
    return np.asarray(vec, dtype=np.float32).tobytes()


def bytes_to_vec(data: bytes) -> list[float]:
    """Deserialize bytes from SQLite BLOB back to a vector."""
    return np.frombuffer(data, dtype=np.float32).tolist()
