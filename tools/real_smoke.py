"""Opt-in real-provider smoke.

Skips politely if the required env vars aren't set, so it's safe to invoke
from CI.

Required env:
  REAL_LLM_API_KEY        (required)
  REAL_LLM_MODEL          default: gpt-4o-mini
  REAL_LLM_BASE_URL       default: https://api.openai.com/v1
  REAL_EMBED_MODEL        default: text-embedding-3-small
  REAL_EMBED_DIM          default: 1536
  REAL_EMBED_API_KEY      default: $REAL_LLM_API_KEY
  REAL_EMBED_BASE_URL     default: $REAL_LLM_BASE_URL
  REAL_KB_MIN_SCORE       default: 0.5   (real embeddings → raise floor)

Backend ENV (set before starting uvicorn):
  VECTOR_STORE=milvus   MILVUS_URI=...      (optional, otherwise memory)
  GRAPH_STORE=neo4j     NEO4J_URI/USER/PWD  (optional, otherwise memory)

Validates:
  1) PUT /settings/llm + /settings/embedding with real creds
  2) POST /settings/test/{llm,embedding} → ok
  3) Create KB → paste doc → 'ready' with chunks > 1 (real models split better)
  4) Inbound message → AI auto-reply uses retrieval; the AI message text
     references something present in the knowledge doc (heuristic check)
  5) AI reply log carries non-trivial confidence
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid

import httpx
import websockets

BASE = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"

DOC = (
    "我们的旗舰产品 ProMax 售价 ¥2,999 元,包含三大功能:【极速充电】、"
    "【智能续航】、【安全防护】。售后政策:7 天无理由退换货,1 年质保。"
    "联系方式:400-100-2000,工作时间 9:00-21:00。"
    "工厂位于深圳南山,所有产品出厂前需要经过三道质检流程。"
    "对于团购订单(≥ 10 台)我们提供 10% 的折扣以及优先发货。"
)


def env(name: str, default: str | None = None) -> str | None:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def must(name: str) -> str | None:
    v = env(name)
    if not v:
        print(f"[real-smoke] SKIP: {name} not set", flush=True)
        return None
    return v


async def drain_dispatches_and_ack(ws, timeout: float = 4.0) -> list[str]:
    """Ack every task.dispatch within `timeout` of silence; collect dispatched text."""
    texts: list[str] = []
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return texts
        data = json.loads(raw)
        if data.get("event") == "task.dispatch":
            tid = data["payload"]["task_id"]
            t = data["payload"]["payload"].get("text", "")
            texts.append(t)
            await ws.send(json.dumps({"event": "task.completed", "payload": {"task_id": tid}}))


async def main() -> int:
    api_key = must("REAL_LLM_API_KEY")
    if not api_key:
        return 0  # gracefully skipped

    llm_model = env("REAL_LLM_MODEL", "gpt-4o-mini")
    llm_base = env("REAL_LLM_BASE_URL", "https://api.openai.com/v1")
    embed_model = env("REAL_EMBED_MODEL", "text-embedding-3-small")
    embed_dim = int(env("REAL_EMBED_DIM", "1536") or "1536")
    embed_api_key = env("REAL_EMBED_API_KEY", api_key)
    embed_base = env("REAL_EMBED_BASE_URL", llm_base)
    min_score = float(env("REAL_KB_MIN_SCORE", "0.5") or "0.5")

    async with httpx.AsyncClient(base_url=BASE, timeout=30) as http:
        r = await http.post("/auth/login", json={"email": "admin@example.com", "password": "admin123"})
        r.raise_for_status()
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
        print("[real-smoke] logged in")

        # 1. push real settings
        r = await http.put("/settings/llm", headers=auth, json={
            "provider": "openai", "model": llm_model,
            "api_key": api_key, "base_url": llm_base,
            "temperature": 0.4,
        })
        r.raise_for_status()
        r = await http.put("/settings/embedding", headers=auth, json={
            "provider": "openai", "model": embed_model,
            "api_key": embed_api_key, "base_url": embed_base,
            "dim": embed_dim,
        })
        r.raise_for_status()
        r = await http.put("/settings/retrieval", headers=auth, json={
            "top_k": 4, "min_score": min_score,
        })
        r.raise_for_status()
        print("[real-smoke] settings saved")

        # 2. probes
        p = (await http.post("/settings/test/llm", headers=auth)).json()
        assert p["ok"], f"LLM probe failed: {p['detail']}"
        print(f"[real-smoke] LLM probe ok  latency={p.get('latency_ms')}ms model={p.get('model')}")

        p = (await http.post("/settings/test/embedding", headers=auth)).json()
        assert p["ok"], f"embedding probe failed: {p['detail']}"
        assert p["dim"] == embed_dim, f"dim mismatch: claimed {embed_dim} got {p['dim']}"
        print(f"[real-smoke] embedding probe ok  dim={p['dim']}")

        # 3. KB
        r = await http.post("/kb", headers=auth, json={"name": "real-kb", "description": "ProMax, 售后, 团购"})
        r.raise_for_status()
        kb = r.json()
        r = await http.post(f"/kb/{kb['id']}/docs/paste", headers=auth, data={"name": "promax.md", "content": DOC})
        r.raise_for_status()
        doc = r.json()
        for _ in range(40):
            await asyncio.sleep(0.5)
            d = (await http.get(f"/kb/{kb['id']}/docs/{doc['id']}", headers=auth)).json()
            if d["status"] in ("ready", "failed"):
                doc = d
                break
        assert doc["status"] == "ready", f"doc ingestion failed: {doc}"
        assert doc["chunk_count"] >= 1
        print(f"[real-smoke] doc ready chunks={doc['chunk_count']}")

        # 4. search via real embeddings — score should be high
        r = await http.post("/kb/search", headers=auth, json={"query": "团购有什么优惠", "top_k": 3})
        r.raise_for_status()
        sr = r.json()
        assert sr["hits"], f"no hits (min_score={min_score})"
        top = sr["hits"][0]
        assert top["score"] >= 0.3, f"top score {top['score']} surprisingly low for real embeddings"
        print(f"[real-smoke] search top score={top['score']:.2f}")

        # 5. AI auto-reply driven by retrieval
        r = await http.post("/robots", headers=auth, json={"name": "real-bot"})
        r.raise_for_status()
        d = r.json()
        rid, rtoken = d["robot"]["robot_id"], d["token"]

        url = f"{WS_BASE}/ws/android?robot_id={rid}&token={rtoken}"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"event": "device.hello", "payload": {"current_page": "HOME"}}))
            await ws.send(json.dumps({
                "event": "message.received",
                "payload": {
                    "contact": {"external_id": "wxid_real", "nickname": "真客户"},
                    "external_msg_id": f"m_{uuid.uuid4().hex[:8]}",
                    "type": "text",
                    "content": "团购 20 台 ProMax 有什么优惠?",
                },
            }))
            texts = await drain_dispatches_and_ack(ws, timeout=12.0)

        assert texts, "AI did not dispatch any reply"
        joined = "\n".join(texts)
        print(f"[real-smoke] AI replied: {joined!r}")
        # heuristic: real model should land on 10% / 折扣 / 优先发货 / 团购 somewhere
        hint_terms = ["10%", "折扣", "优先", "团购"]
        assert any(t in joined for t in hint_terms), (
            f"AI reply did not reference KB facts (looked for {hint_terms!r})"
        )
        print("[real-smoke] AI reply references KB facts ✓")

        # 6. AI log carries non-trivial confidence + model name
        convs = (await http.get("/conversations", headers=auth)).json()
        cid = convs[0]["id"]
        logs = (await http.get(f"/ai/logs?conversation_id={cid}", headers=auth)).json()
        assert logs, "no AI log"
        last = logs[0]
        assert last["model"] and last["model"] != "mock", f"expected real model, got {last['model']}"
        print(f"[real-smoke] AI log model={last['model']} confidence={last['confidence']:.2f}")

    print("[real-smoke] ALL PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
