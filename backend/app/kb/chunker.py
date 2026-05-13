"""Sliding-window text chunker.

Operates on **characters**, not tokens, since we mix CJK and ASCII. Cuts on
paragraph / sentence boundaries when convenient, otherwise hard-cuts.
"""
from __future__ import annotations

import re


_BOUNDARY = re.compile(r"(?<=[。!?;\n])")


def chunk(text: str, *, size: int = 400, overlap: int = 60) -> list[str]:
    text = (text or "").strip()
    if not text:
        return []
    if len(text) <= size:
        return [text]

    pieces = _BOUNDARY.split(text)
    pieces = [p for p in pieces if p]

    chunks: list[str] = []
    buf = ""
    for p in pieces:
        # if a single piece is huge, hard-cut it
        while len(p) > size:
            head, p = p[:size], p[size:]
            if buf:
                chunks.append(buf)
                buf = ""
            chunks.append(head)
        if len(buf) + len(p) <= size:
            buf += p
        else:
            chunks.append(buf)
            # overlap tail
            tail = buf[-overlap:] if overlap > 0 else ""
            buf = tail + p
    if buf:
        chunks.append(buf)
    return [c.strip() for c in chunks if c.strip()]
