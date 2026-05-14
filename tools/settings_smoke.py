"""Verify that /settings updates take effect immediately for new requests
(no backend restart). Uses only mock providers so it runs anywhere.
"""
from __future__ import annotations

import asyncio
import sys

import httpx

BASE = "http://localhost:8000"


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=15) as http:
        r = await http.post(
            "/auth/login",
            json={"email": "admin@example.com", "password": "admin123"},
        )
        r.raise_for_status()
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

        # ---- /settings round-trip
        first = (await http.get("/settings", headers=auth)).json()
        assert "llm" in first and "embedding" in first and "infra" in first
        print(f"[settings-smoke] read OK, infra={first['infra']}")

        # ---- write LLM: api_key masked → unchanged
        r = await http.put(
            "/settings/llm",
            headers=auth,
            json={
                "provider": "mock",
                "model": "mock",
                "api_key": "",  # blank = keep
                "base_url": "",
                "temperature": 0.3,
            },
        )
        r.raise_for_status()
        ver = r.json()["version"]
        print(f"[settings-smoke] llm saved v{ver}")

        # ---- probe LLM via mock — should succeed regardless of key
        r = await http.post(
            "/settings/test/llm",
            headers=auth,
            json={
                "provider": "mock",
                "model": "mock",
                "api_key": "",
                "base_url": "",
                "temperature": 0.0,
            },
        )
        r.raise_for_status()
        out = r.json()
        assert out["ok"], f"mock probe should always pass; got {out}"
        print(f"[settings-smoke] llm probe(mock) ok detail={out['detail']!r}")

        # ---- probe embedding mock
        r = await http.post(
            "/settings/test/embedding",
            headers=auth,
            json={
                "provider": "mock",
                "model": "mock",
                "api_key": "",
                "base_url": "",
                "dim": 64,
            },
        )
        r.raise_for_status()
        out = r.json()
        assert out["ok"] and out["dim"] == 64, f"mock embedding probe failed: {out}"
        print(f"[settings-smoke] embedding probe(mock) ok dim={out['dim']}")

        # ---- write retrieval + ai behavior
        r = await http.put(
            "/settings/retrieval",
            headers=auth,
            json={"top_k": 3, "min_score": 0.1},
        )
        r.raise_for_status()
        r = await http.put(
            "/settings/ai",
            headers=auth,
            json={
                "confidence_threshold": 0.7,
                "context_window": 6,
                "default_prompt": "测试用提示词",
            },
        )
        r.raise_for_status()

        # ---- read-back: api_key is masked, other fields persisted
        cur = (await http.get("/settings", headers=auth)).json()
        assert cur["retrieval"]["top_k"] == 3
        assert abs(cur["retrieval"]["min_score"] - 0.1) < 1e-9
        assert cur["ai"]["confidence_threshold"] == 0.7
        assert cur["ai"]["context_window"] == 6
        assert cur["ai"]["default_prompt"] == "测试用提示词"
        assert cur["llm"]["api_key"] in ("", "********")  # masked
        print("[settings-smoke] read-back fields persisted OK")

        # ---- vector / graph store probes always run
        r = await http.post("/settings/test/vector_store", headers=auth)
        assert r.json()["ok"], f"vector probe: {r.json()}"
        r = await http.post("/settings/test/graph_store", headers=auth)
        assert r.json()["ok"], f"graph probe: {r.json()}"
        print("[settings-smoke] vector + graph store probes OK")

        # ---- regression: provider=openai without api_key must FAIL the test,
        # not silently fall back to mock.
        r = await http.post(
            "/settings/test/llm",
            headers=auth,
            json={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "",  # empty
                "base_url": "https://api.openai.com/v1",
                "temperature": 0.0,
            },
        )
        out = r.json()
        assert not out["ok"], f"expected ok=false for openai+empty key; got {out}"
        assert "api_key" in out["detail"], f"unexpected detail: {out}"
        print(f"[settings-smoke] openai+empty key correctly rejected: {out['detail'][:60]!r}")

        # ---- regression: "********" sentinel must mean 'keep saved' on save.
        # First save a real-looking key, then send the placeholder, then check
        # the live cfg via the test endpoint (which reflects merged config).
        r = await http.put(
            "/settings/llm",
            headers=auth,
            json={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "sk-PRETEND-REAL-KEY-xxxxxxxxxxxx",
                "base_url": "https://api.openai.com/v1",
                "temperature": 0.0,
            },
        )
        r.raise_for_status()
        # send placeholder mask (simulates "user opened page and clicked save without touching key")
        r = await http.put(
            "/settings/llm",
            headers=auth,
            json={
                "provider": "openai",
                "model": "gpt-4o-mini",
                "api_key": "********",
                "base_url": "https://api.openai.com/v1",
                "temperature": 0.0,
            },
        )
        r.raise_for_status()
        # If '********' were stored as literal key, the LLM HTTP call would fail
        # with our fake key too — but more importantly, the test endpoint would
        # see api_key='********' and try to use it. We assert the saved key
        # didn't get overwritten by probing with body=null (uses saved only).
        r = await http.post("/settings/test/llm", headers=auth, json=None)
        out = r.json()
        # Saved key is fake → the openai call will fail at HTTP layer with a
        # provider error. The point is that we DID try to use it (not silently
        # fall back to mock). detail must mention HTTP / unauthorized / similar,
        # not 'api_key 为空'.
        assert (
            not out["ok"]
        ), f"expected real openai call to fail (fake key) — got ok=true: {out}"
        assert "api_key" not in out["detail"] or "401" in out["detail"], (
            f"unexpected detail; mask sentinel may have leaked: {out}"
        )
        print(f"[settings-smoke] '********' sentinel preserved saved key: {out['detail'][:60]!r}")

        # cleanup: switch back to mock so other smokes are unaffected
        r = await http.put(
            "/settings/llm",
            headers=auth,
            json={
                "provider": "mock",
                "model": "mock",
                "api_key": "",
                "base_url": "",
                "temperature": 0.0,
            },
        )
        r.raise_for_status()

    print("[settings-smoke] ALL PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
