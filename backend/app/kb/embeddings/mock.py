"""Deterministic mock embedding.

Builds a bag-of-character-bigrams vector — same input always yields the
same vector, similar inputs yield similar vectors. Good enough to make the
RAG plumbing meaningful in tests/demo without any external API.
"""
from __future__ import annotations

import hashlib
import math

from .base import EmbeddingProvider


class MockEmbedding(EmbeddingProvider):
    name = "mock"

    def __init__(self, dim: int = 256) -> None:
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        return [_embed(t, self.dim) for t in texts]


def _embed(text: str, dim: int) -> list[float]:
    vec = [0.0] * dim
    text = text.strip().lower()
    if not text:
        return _normalize(vec) or [1.0 / math.sqrt(dim)] * dim
    # character bigrams + unigrams
    tokens = list(text)
    for i in range(len(tokens)):
        unit = tokens[i]
        _add(vec, unit, dim, weight=1.0)
        if i + 1 < len(tokens):
            _add(vec, tokens[i] + tokens[i + 1], dim, weight=1.5)
    return _normalize(vec)


def _add(vec: list[float], token: str, dim: int, weight: float) -> None:
    h = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
    idx = int.from_bytes(h[:4], "little") % dim
    sign = 1.0 if h[4] & 1 else -1.0
    vec[idx] += sign * weight


def _normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]
