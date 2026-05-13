"""Deterministic mock LLM.

Lets the whole AI workflow run end-to-end without any external API key —
useful for tests, demos, and CI. The replies are intentionally generic but
shaped enough to look like a real assistant, so the UX flow can be validated.
"""
from __future__ import annotations

import re
import time

from .base import ChatMessage, LLMProvider, LLMResult


class MockProvider(LLMProvider):
    name = "mock"

    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> LLMResult:
        t0 = time.perf_counter()
        # last user message is the trigger
        user = next(
            (m.content for m in reversed(messages) if m.role == "user"),
            "",
        )
        text, confidence = _craft(user)
        latency_ms = max(1, int((time.perf_counter() - t0) * 1000))
        return LLMResult(text=text, confidence=confidence, model="mock", latency_ms=latency_ms)


_RULES: list[tuple[re.Pattern[str], str, float]] = [
    (re.compile(r"(在吗|有人吗|hello|hi)", re.I), "您好,在的~请问有什么可以帮您?", 0.92),
    (re.compile(r"(多少钱|价格|报价)"), "好的,我这边给您看一下价格,请稍等。", 0.78),
    (re.compile(r"(退款|投诉|差评|不满意)"), "非常抱歉给您带来不便,我马上为您处理。", 0.45),  # low → escalate
    (re.compile(r"(谢谢|感谢)"), "不客气,有任何问题随时找我~", 0.9),
]


def _craft(user_text: str) -> tuple[str, float]:
    text = (user_text or "").strip()
    if not text:
        return ("您好,请问有什么可以帮您?", 0.6)
    for pat, reply, conf in _RULES:
        if pat.search(text):
            return reply, conf
    # generic fallback — low confidence so mixed mode will escalate
    return (f"收到您说的「{text[:30]}」,我马上确认一下。", 0.5)
