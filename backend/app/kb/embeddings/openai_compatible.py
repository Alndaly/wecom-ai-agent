from __future__ import annotations

import httpx

from .base import EmbeddingProvider


class OpenAIEmbedding(EmbeddingProvider):
    name = "openai"

    def __init__(self, *, api_key: str, base_url: str, model: str, dim: int = 1536) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dim = dim

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not self.api_key:
            raise RuntimeError("EMBEDDING_API_KEY (or LLM_API_KEY) empty")
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(
                f"{self.base_url}/embeddings",
                json={"model": self.model, "input": texts},
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        r.raise_for_status()
        data = r.json()
        return [d["embedding"] for d in data["data"]]
