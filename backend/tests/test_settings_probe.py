from __future__ import annotations

from app.routers.settings import _merge_for_test


def test_merge_for_test_uses_requested_active_profile():
    saved = {
        "provider": "openai",
        "model": "gemma4",
        "api_key": "saved-key",
        "base_url": "http://localhost:11434/v1",
    }
    body = {
        "provider": "openai",
        "model": "gemma4",
        "api_key": "",
        "base_url": "http://localhost:11434/v1",
        "active_profile": "fallback",
        "profiles": [
            {
                "id": "main",
                "name": "gemma4",
                "provider": "openai",
                "model": "gemma4",
                "api_key": "",
                "base_url": "http://localhost:11434/v1",
            },
            {
                "id": "fallback",
                "name": "openai",
                "provider": "openai",
                "model": "gpt-5.5",
                "api_key": "fallback-key",
                "base_url": "https://147ai.com/v1",
            },
        ],
    }

    cfg = _merge_for_test(saved, body)

    assert cfg["model"] == "gpt-5.5"
    assert cfg["api_key"] == "fallback-key"
    assert cfg["base_url"] == "https://147ai.com/v1"
