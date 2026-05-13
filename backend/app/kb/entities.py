"""Lightweight entity extraction.

For MVP3 we use deterministic patterns + keyword inventory loaded from KB
metadata (`description` can carry comma-separated seed terms). Replaceable
with an LLM-driven extractor later.

Returns a list of `(label, name)` tuples.
"""
from __future__ import annotations

import re


# Recognise:
#   - 价格 / pricing : matches numbers + 元/￥
#   - product names: caller passes a list of known seeds
#   - features: bullet-like uppercased segments
_PRICE_RX = re.compile(r"(?:¥|￥|\$|\b)\s*\d+(?:[.,]\d+)?\s*(?:元|RMB|CNY|USD|美元)?", re.I)
_FEATURE_RX = re.compile(r"【([^】]{2,20})】|\[([A-Za-z0-9_\-]{2,30})\]")


def extract(text: str, *, product_seeds: list[str] | None = None) -> list[tuple[str, str]]:
    found: set[tuple[str, str]] = set()
    text_lower = text.lower()

    if product_seeds:
        for seed in product_seeds:
            s = seed.strip()
            if not s:
                continue
            if s.lower() in text_lower:
                found.add(("Product", s.lower()))

    for m in _PRICE_RX.finditer(text):
        found.add(("Price", m.group(0).strip().lower()))

    for m in _FEATURE_RX.finditer(text):
        name = (m.group(1) or m.group(2) or "").strip()
        if name:
            found.add(("Feature", name.lower()))

    return sorted(found)
