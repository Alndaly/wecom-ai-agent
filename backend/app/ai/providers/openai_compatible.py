"""OpenAI-compatible HTTP provider.

Works with OpenAI proper plus any compatible endpoint (Azure-OpenAI,
DeepSeek, 通义/Qwen on DashScope-compatible mode, Zhipu, Ollama via
its /v1 shim, etc.) by overriding `base_url`.

Confidence is heuristic — derived from response logprobs if present,
otherwise from `finish_reason` and length.
"""
from __future__ import annotations

import logging
import time

import httpx

from .base import ChatMessage, LLMProvider, LLMResult

log = logging.getLogger(__name__)


class OpenAICompatibleProvider(LLMProvider):
    name = "openai"

    def __init__(self, *, api_key: str, base_url: str, model: str) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> LLMResult:
        if not self.api_key:
            raise RuntimeError("LLM_API_KEY is empty; set it or switch LLM_PROVIDER=mock")

        body = {
            "model": self.model,
            "messages": [{"role": m.role, "content": m.content} for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=30) as http:
            r = await http.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        data = r.json()
        choice = data["choices"][0]
        text = choice["message"]["content"]
        finish = choice.get("finish_reason", "")
        # naive heuristic confidence
        conf = 0.8 if finish == "stop" else 0.55
        if not text.strip():
            conf = 0.3
        return LLMResult(text=text, confidence=conf, model=self.model, latency_ms=latency_ms, raw=data)
