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
            raise RuntimeError("LLM api_key is empty; configure it via /settings or env")

        body = {
            "model": self.model,
            "messages": [_serialize_message(m) for m in messages],
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        t0 = time.perf_counter()
        async with httpx.AsyncClient(timeout=60) as http:
            r = await http.post(
                f"{self.base_url}/chat/completions",
                json=body,
                headers={"Authorization": f"Bearer {self.api_key}"},
            )
        latency_ms = int((time.perf_counter() - t0) * 1000)
        if r.status_code >= 400:
            raise RuntimeError(f"LLM HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        try:
            choice = data["choices"][0]
            text = choice["message"]["content"] or ""
            finish = choice.get("finish_reason", "")
        except (KeyError, IndexError) as e:
            raise RuntimeError(f"unexpected LLM response shape: {data!r}") from e
        # heuristic confidence; finish_reason=stop is the green-light signal
        conf = 0.8 if finish == "stop" else 0.55
        if not text.strip():
            conf = 0.3
        return LLMResult(text=text, confidence=conf, model=self.model, latency_ms=latency_ms, raw=data)


def _serialize_message(m: ChatMessage) -> dict:
    """Pack a ChatMessage into the OpenAI Chat Completions wire format.

    Plain text turns stay as `{"role", "content": "..."}` (string). Turns with
    attached images use the multimodal list shape required by gpt-4o / qwen-vl
    / glm-4v etc.: `[{"type": "text", "text": ...}, {"type": "image_url", ...}]`.
    """
    if not m.images:
        return {"role": m.role, "content": m.content}
    parts: list[dict] = [{"type": "text", "text": m.content}]
    for mime, b64 in m.images:
        parts.append({
            "type": "image_url",
            "image_url": {"url": f"data:{mime};base64,{b64}"},
        })
    return {"role": m.role, "content": parts}
