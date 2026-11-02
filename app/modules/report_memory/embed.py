"""Question similarity: a local multilingual model (meaning, across Hindi/English) plus character n-grams
(spelling, Hinglish variants). The model loads on first use, uses at most ``threads`` CPU threads and,
if it cannot load, similarity continues with n-grams only. Nothing leaves this machine.
"""
from __future__ import annotations

import re
import threading
import unicodedata
import zlib

import numpy as np

from app.core.logger import get_logger

logger = get_logger("module.report_memory.embed")
LEXICAL_DIMS = 4096
SEMANTIC_WEIGHT = 0.7


def normalize(text: str) -> str:
    """Case, width, punctuation and spacing differences do not make two questions different."""
    text = unicodedata.normalize("NFKC", text or "").casefold()
    text = re.sub(r"[^\wऀ-ॿ]+", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def lexical_vector(text: str) -> np.ndarray:
    """Hashed character 3- and 4-grams, L2-normalised."""
    padded = f" {normalize(text)} "
    vector = np.zeros(LEXICAL_DIMS, dtype=np.float32)
    for size in (3, 4):
        for i in range(len(padded) - size + 1):
            vector[zlib.crc32(padded[i:i + size].encode()) % LEXICAL_DIMS] += 1.0
    norm = float(np.linalg.norm(vector))
    return vector / norm if norm else vector


class Embedder:
    def __init__(self, model_name: str, semantic: bool = True, threads: int = 2) -> None:
        self.model_name, self.semantic, self.threads = model_name, semantic, threads
        self._model = None
        self._failed = not semantic
        self._lock = threading.Lock()

    @property
    def semantic_ready(self) -> bool:
        return self._model is not None

    def _load(self):
        with self._lock:
            if self._model is None and not self._failed:
                try:
                    import torch
                    torch.set_num_threads(max(1, self.threads))
                    from sentence_transformers import SentenceTransformer
                    self._model = SentenceTransformer(self.model_name, device="cpu")
                    logger.info("multilingual model loaded: %s", self.model_name)
                except Exception as exc:  # missing package, no download access, out of memory ...
                    self._failed = True
                    logger.error("multilingual model unavailable (%s); using spelling similarity only",
                                 type(exc).__name__)
            return self._model

    def semantic_vector(self, text: str) -> np.ndarray | None:
        model = self._load()
        if model is None:
            return None
        with self._lock:
            return np.asarray(model.encode(normalize(text), normalize_embeddings=True), dtype=np.float32)


def similarity(lex_a, sem_a, lex_b, sem_b) -> float:
    lexical = float(np.dot(lex_a, lex_b))
    if sem_a is None or sem_b is None:
        return lexical
    semantic = float(np.dot(sem_a, sem_b))
    # Across scripts (Hindi vs English) spelling overlap is ~0 and must not pull a clear meaning match down.
    return max(semantic, SEMANTIC_WEIGHT * semantic + (1 - SEMANTIC_WEIGHT) * lexical)
