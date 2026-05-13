"""MVP2 AI smoke — auto-reply + suggest (mixed mode + low confidence).

Flow:
  1. login, create robot, open Android WS.
  2. Set conversation to mode=ai. Send "在吗" (high-confidence rule in mock).
     Expect: task.dispatch carrying the AI text; auto-ack; message status=sent;
     out message sender_type=ai.
  3. Set mode=mixed, send "我要投诉" (low confidence). Expect: NO task.dispatch,
     but ai.suggestion event would be broadcast — we verify via AI logs API
     that the latest log has action=suggest.

Run with backend started fresh:
    rm backend/dev.db
    uvicorn app.main:app --port 8000  &
    backend/.venv/bin/python tools/ai_smoke.py
"""
from __future__ import annotations

import asyncio
import json
import sys
import uuid
from datetime import datetime, timezone

import httpx
import websockets

BASE = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"


async def expect_dispatch(ws, timeout: float = 3.0) -> dict | None:
    try:
        while True:
            raw = await asyncio.wait_for(ws.recv(), timeout=timeout)
            data = json.loads(raw)
            if data.get("event") == "task.dispatch":
                return data["payload"]
    except asyncio.TimeoutError:
        return None


async def drain_dispatches_and_ack(ws, timeout: float = 1.5) -> int:
    """Ack every task.dispatch arriving within `timeout` of silence."""
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
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

        r = await http.post("/robots", headers=auth, json={"name": "ai-bot"})
        r.raise_for_status()
        d = r.json()
        rid, rpk, rtoken = d["robot"]["robot_id"], d["robot"]["id"], d["token"]
        print(f"[ai-smoke] robot {rid}")

        url = f"{WS_BASE}/ws/android?robot_id={rid}&token={rtoken}"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"event": "device.hello", "payload": {"current_page": "HOME"}}))

            # ---------- case 1: mode=ai, "在吗" → AI replies ----------
            await ws.send(json.dumps({
                "event": "message.received",
                "payload": {
                    "contact": {"external_id": "wxid_aitest", "nickname": "AI客户"},
                    "external_msg_id": f"m_{uuid.uuid4().hex[:8]}",
                    "type": "text",
                    "content": "在吗",
                },
            }))
            await asyncio.sleep(0.6)

            # find conversation, set to mode=ai
            convs = (await http.get("/conversations", headers=auth)).json()
            conv = convs[0]
            await http.patch(f"/conversations/{conv['id']}", headers=auth, json={"mode": "ai"})

            # send another inbound msg now under mode=ai
            await ws.send(json.dumps({
                "event": "message.received",
                "payload": {
                    "contact": {"external_id": "wxid_aitest", "nickname": "AI客户"},
                    "external_msg_id": f"m_{uuid.uuid4().hex[:8]}",
                    "type": "text",
                    "content": "hello",
                },
            }))

            # ack every dispatch the backend sent (could be 1 or 2 — one per inbound)
            acked = await drain_dispatches_and_ack(ws, timeout=2.0)
            assert acked >= 1, "expected at least one AI task.dispatch"
            print(f"[ai-smoke] AI dispatched & acked {acked} task(s)")
            await asyncio.sleep(0.3)

            msgs = (await http.get(f"/conversations/{conv['id']}/messages?limit=100", headers=auth)).json()
            ai_out = [m for m in msgs if m["direction"] == "out" and m["sender_type"] == "ai"]
            assert ai_out, "no AI-sent message found"
            sent_ai = [m for m in ai_out if m["status"] == "sent"]
            assert sent_ai, f"no AI message in 'sent' status; statuses={[m['status'] for m in ai_out]}"
            print(f"[ai-smoke] AI message(s) sent: {len(sent_ai)} OK")

            # ---------- case 2: mode=mixed + 投诉 → suggest only ----------
            await http.patch(f"/conversations/{conv['id']}", headers=auth, json={"mode": "mixed"})
            await ws.send(json.dumps({
                "event": "message.received",
                "payload": {
                    "contact": {"external_id": "wxid_aitest", "nickname": "AI客户"},
                    "external_msg_id": f"m_{uuid.uuid4().hex[:8]}",
                    "type": "text",
                    "content": "我要投诉,你们的服务太差了!",
                },
            }))

            dispatch = await expect_dispatch(ws, timeout=2.0)
            assert dispatch is None, "AI should NOT auto-send a low-confidence reply in mixed mode"
            print("[ai-smoke] mixed+low confidence → no auto-send OK")

            logs = (await http.get(f"/ai/logs?conversation_id={conv['id']}", headers=auth)).json()
            assert logs, "no AI logs"
            latest = logs[0]
            assert latest["action"] == "suggest", f"expected suggest, got {latest['action']}"
            print(f"[ai-smoke] AI log latest action=suggest confidence={latest['confidence']:.2f} OK")

    print("[ai-smoke] ALL PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
