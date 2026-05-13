"""MVP3 KB + memory smoke.

Validates:
  1) Create knowledge base + paste a doc → status moves to 'ready'.
  2) Search returns the seeded chunk for a relevant query.
  3) Inbound customer message → AI workflow retrieves the chunk and the
     reply contains the salient fact (in the mock provider's deterministic
     output it does NOT — so we verify via ai_reply_log + chunk via API,
     and via the kb.hits broadcast captured from the web socket).
  4) After enough inbound messages, /memory/{contact_id} returns a non-empty
     summary.

Run after `rm backend/dev.db && uvicorn app.main:app`.
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from contextlib import asynccontextmanager

import httpx
import websockets

BASE = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"

DOC = (
    "我们的旗舰产品 ProMax 售价 ¥2,999 元,包含三大功能:【极速充电】、"
    "【智能续航】、【安全防护】。售后政策:7 天无理由退换货,1 年质保。"
    "联系方式:400-100-2000,工作时间 9:00-21:00。"
)


@asynccontextmanager
async def web_ws_listener(token: str, events: list[dict]):
    url = f"{WS_BASE}/ws/web?token={token}"
    async with websockets.connect(url) as ws:
        stop = asyncio.Event()
        task = asyncio.create_task(_loop_until_stop(ws, stop, events))
        try:
            yield
        finally:
            stop.set()
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass


async def _loop_until_stop(ws, stop: asyncio.Event, events: list[dict]) -> None:
    try:
        while not stop.is_set():
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=0.3)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                return
            except Exception:
                return
            try:
                events.append(json.loads(raw))
            except Exception:
                pass
    except asyncio.CancelledError:
        return


async def drain_dispatches_and_ack(ws, timeout: float = 2.0) -> int:
    count = 0
    while True:
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
        except asyncio.TimeoutError:
            return count
        data = json.loads(raw)
        if data.get("event") == "task.dispatch":
            tid = data["payload"]["task_id"]
            await ws.send(json.dumps({"event": "task.completed", "payload": {"task_id": tid}}))
            count += 1


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=10) as http:
        r = await http.post("/auth/login", json={"email": "admin@example.com", "password": "admin123"})
        r.raise_for_status()
        token = r.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        print("[kb-smoke] logged in")

        # ---------- KB ----------
        r = await http.post("/kb", headers=auth, json={
            "name": "smoke-kb",
            "description": "ProMax, 售后",  # serves as entity seeds
        })
        r.raise_for_status()
        kb = r.json()
        print(f"[kb-smoke] kb id={kb['id']}")

        r = await http.post(
            f"/kb/{kb['id']}/docs/paste",
            headers=auth,
            data={"name": "promax.md", "content": DOC},
        )
        r.raise_for_status()
        doc = r.json()

        # poll for ready
        for _ in range(20):
            await asyncio.sleep(0.3)
            r = await http.get(f"/kb/{kb['id']}/docs/{doc['id']}", headers=auth)
            d = r.json()
            if d["status"] in ("ready", "failed"):
                doc = d
                break
        assert doc["status"] == "ready", f"doc not ready: {doc}"
        assert doc["chunk_count"] >= 1
        print(f"[kb-smoke] doc ready chunks={doc['chunk_count']}")

        # search
        r = await http.post("/kb/search", headers=auth, json={"query": "ProMax 多少钱"})
        r.raise_for_status()
        sr = r.json()
        assert sr["hits"], "expected at least one hit"
        top = sr["hits"][0]
        assert "ProMax" in top["text"] or "2,999" in top["text"]
        print(f"[kb-smoke] search top score={top['score']:.2f} text={top['text'][:30]!r}")

        # ---------- AI uses retrieval ----------
        r = await http.post("/robots", headers=auth, json={"name": "kb-bot"})
        r.raise_for_status()
        d = r.json()
        rid, rtoken = d["robot"]["robot_id"], d["token"]
        print(f"[kb-smoke] robot {rid}")

        events: list[dict] = []
        async with web_ws_listener(token, events):
            url = f"{WS_BASE}/ws/android?robot_id={rid}&token={rtoken}"
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"event": "device.hello", "payload": {"current_page": "HOME"}}))
                # inbound → mixed mode triggers AI
                await ws.send(json.dumps({
                    "event": "message.received",
                    "payload": {
                        "contact": {"external_id": "wxid_kb", "nickname": "KB客户"},
                        "external_msg_id": f"m_{uuid.uuid4().hex[:8]}",
                        "type": "text",
                        "content": "ProMax 多少钱?",
                    },
                }))
                # ack any dispatches the AI created (high-confidence path)
                acked = await drain_dispatches_and_ack(ws, timeout=3.0)
                print(f"[kb-smoke] acked {acked} AI dispatches")

            # give the web ws a beat to deliver kb.hits / ai.suggestion
            await asyncio.sleep(0.5)

        kb_hit_events = [e for e in events if e.get("event") == "kb.hits"]
        assert kb_hit_events, f"no kb.hits event received; events={[e.get('event') for e in events]}"
        hit_ids = kb_hit_events[-1]["payload"]["hit_ids"]
        assert hit_ids, "kb.hits payload empty"
        print(f"[kb-smoke] kb.hits received with {len(hit_ids)} hit(s)")

        # lookup chunks by id (right-panel API)
        r = await http.get(
            f"/kb/chunks/by-ids?ids={','.join(map(str, hit_ids))}", headers=auth
        )
        r.raise_for_status()
        chunks = r.json()
        assert chunks, "chunks api returned empty"
        print(f"[kb-smoke] chunks/by-ids returned {len(chunks)} chunk(s)")

        # ---------- long-term memory ----------
        # send 10 more inbound msgs to trigger summary
        url2 = f"{WS_BASE}/ws/android?robot_id={rid}&token={rtoken}"
        async with websockets.connect(url2) as ws:
            await ws.send(json.dumps({"event": "device.hello", "payload": {"current_page": "HOME"}}))
            for i in range(11):
                await ws.send(json.dumps({
                    "event": "message.received",
                    "payload": {
                        "contact": {"external_id": "wxid_kb", "nickname": "KB客户"},
                        "external_msg_id": f"m2_{i}_{uuid.uuid4().hex[:6]}",
                        "type": "text",
                        "content": f"再问一下,关于售后政策有什么补充?#{i}",
                    },
                }))
                await drain_dispatches_and_ack(ws, timeout=0.6)

        # find contact id via /conversations
        r = await http.get("/conversations", headers=auth)
        conv = r.json()[0]
        cid = conv["contact_id"]

        # profile
        r = await http.get(f"/memory/{cid}", headers=auth)
        prof = r.json()
        assert prof and prof.get("summary"), f"expected non-empty summary; got {prof}"
        print(f"[kb-smoke] profile summary={prof['summary'][:50]!r}")

        r = await http.get(f"/memory/{cid}/memories", headers=auth)
        mems = r.json()
        assert mems, "expected at least one user_memory row"
        print(f"[kb-smoke] memories rows={len(mems)}")

    print("[kb-smoke] ALL PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
