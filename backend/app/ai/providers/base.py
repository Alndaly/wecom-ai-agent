from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal


@dataclass
class ChatMessage:
    role: Literal["system", "user", "assistant"]
    content: str
    # Optional inline image attachments. Each entry is base64-encoded data
    # (without the `data:<mime>;base64,` prefix) — providers wrap it as
    # required by their wire protocol. Empty for text-only turns.
    images: list[tuple[str, str]] = field(default_factory=list)
    # (mime, base64_data) tuples — e.g. ("image/jpeg", "...")


    def __post_init__(self) -> None:
        # `images` must be safe to mutate; ensure default is per-instance.
        if self.images is None:
            self.images = []


@dataclass
class LLMResult:
    text: str
    confidence: float
    model: str
    latency_ms: int
    raw: dict | None = None


class LLMProvider(ABC):
    name: str

    @abstractmethod
    async def chat(
        self,
        messages: list[ChatMessage],
        *,
        temperature: float = 0.7,
        max_tokens: int = 512,
    ) -> LLMResult:
        ...
