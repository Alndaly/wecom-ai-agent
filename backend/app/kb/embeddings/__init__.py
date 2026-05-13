from __future__ import annotations

from app.core.config import settings

from .base import EmbeddingProvider
from .mock import MockEmbedding
from .openai_compatible import OpenAIEmbedding

__all__ = ["EmbeddingProvider", "get_embedding_provider"]

_provider: EmbeddingProvider | None = None


def get_embedding_provider() -> EmbeddingProvider:
    global _provider
    if _provider is not None:
        return _provider
    name = settings.embedding_provider.lower()
    if name == "openai":
        _provider = OpenAIEmbedding(
            api_key=settings.embedding_api_key or settings.llm_api_key,
            base_url=settings.embedding_base_url or settings.llm_base_url or "https://api.openai.com/v1",
            model=settings.embedding_model,
        )
    else:
        _provider = MockEmbedding(dim=settings.embedding_dim)
    return _provider
