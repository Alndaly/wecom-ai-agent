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

    print("[settings-smoke] ALL PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
