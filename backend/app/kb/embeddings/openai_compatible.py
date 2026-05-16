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
            raise RuntimeError("embedding api_key empty; configure via /settings or env")
        if not texts:
            return []
        # Batch — provider-side limits vary (OpenAI 2048 inputs / 8k tokens);
        # 64 is a safe chunk for mixed CJK content.
        out: list[list[float]] = []
        async with httpx.AsyncClient(timeout=60) as http:
            for i in range(0, len(texts), 64):
                batch = texts[i : i + 64]
                r = await http.post(
                    f"{self.base_url}/embeddings",
                    json={"model": self.model, "input": batch},
                    headers={"Authorization": f"Bearer {self.api_key}"},
                )
                if r.status_code >= 400:
                    raise RuntimeError(f"embedding HTTP {r.status_code}: {r.text}")
                data = r.json()
                try:
                    out.extend(d["embedding"] for d in data["data"])
                except (KeyError, TypeError) as e:
                    raise RuntimeError(f"unexpected embedding response: {data!r}") from e
        return out
