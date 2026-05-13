"""End-to-end smoke test for MVP1.

Flow:
  1. login as seeded admin
  2. create a robot, capture token
  3. start mock-android (in-process) → backend WS
  4. inbound message via mock → check conversation/message via REST
  5. POST send_message via REST → mock auto-acks → check message status=sent

Run after `uvicorn app.main:app` is up:
    python tools/e2e_smoke.py
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


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=10) as http:
        # 1. login
        r = await http.post("/auth/login", json={"email": "admin@example.com", "password": "admin123"})
        r.raise_for_status()
        token = r.json()["access_token"]
        auth = {"Authorization": f"Bearer {token}"}
        print("[smoke] logged in")

        # 2. create robot
        r = await http.post("/robots", headers=auth, json={"name": "smoke-bot"})
        r.raise_for_status()
        data = r.json()
        robot_id = data["robot"]["robot_id"]
        robot_pk = data["robot"]["id"]
        robot_token = data["token"]
        print(f"[smoke] created robot {robot_id}")

        # 3. open WS as mock android
        url = f"{WS_BASE}/ws/android?robot_id={robot_id}&token={robot_token}"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"event": "device.hello", "payload": {"current_page": "HOME"}}))
            print("[smoke] android ws connected")

            # 4. inbound message
            ext_msg_id = f"mock_{uuid.uuid4().hex[:8]}"
            await ws.send(json.dumps({
                "event": "message.received",
                "payload": {
                    "contact": {"external_id": "wxid_smoke", "nickname": "冒烟客户"},
                    "external_msg_id": ext_msg_id,
                    "type": "text",
                    "content": "你好,在吗?",
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                },
            }))
            await asyncio.sleep(0.5)

            # find the conversation; force mode=human so AI does not autoplay
            r = await http.get("/conversations", headers=auth, params={"robot_id": robot_pk})
            r.raise_for_status()
            convs = r.json()
            assert convs, "no conversation created"
            conv = convs[0]
            await http.patch(f"/conversations/{conv['id']}", headers=auth, json={"mode": "human"})
            print(f"[smoke] conversation id={conv['id']} (forced mode=human)")

            # messages — there may already be an AI reply from the first inbound (default mixed mode)
            r = await http.get(f"/conversations/{conv['id']}/messages", headers=auth)
            msgs = r.json()
            inbound = [m for m in msgs if m["direction"] == "in"]
            assert any(m["content"] == "你好,在吗?" for m in inbound)
            print(f"[smoke] inbound message OK (total msgs={len(msgs)})")
            # drain any AI dispatches already in-flight
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=0.5)
                    data = json.loads(raw)
                    if data.get("event") == "task.dispatch":
                        await ws.send(json.dumps({"event": "task.completed", "payload": {"task_id": data["payload"]["task_id"]}}))
            except asyncio.TimeoutError:
                pass

            # idempotency
            await ws.send(json.dumps({
                "event": "message.received",
                "payload": {
                    "contact": {"external_id": "wxid_smoke", "nickname": "冒烟客户"},
                    "external_msg_id": ext_msg_id,
                    "type": "text",
                    "content": "你好,在吗?",
                },
            }))
            await asyncio.sleep(0.3)
            r = await http.get(f"/conversations/{conv['id']}/messages", headers=auth)
            inbound2 = [m for m in r.json() if m["direction"] == "in"]
            # only one inbound with the original external_msg_id should exist
            assert sum(1 for m in inbound2 if m["content"] == "你好,在吗?") == 1, "dedupe failed"
            print("[smoke] dedupe OK")

            # 5. agent reply
            r = await http.post(
                f"/conversations/{conv['id']}/messages",
                headers=auth,
                json={"type": "text", "content": "你好,我是客服小A"},
            )
            r.raise_for_status()
            sent = r.json()
            task_id = sent["task"]["id"]
            msg_id = sent["message"]["id"]
            print(f"[smoke] sent message id={msg_id} task_id={task_id}")

            # 6. listen for dispatch + ack
            try:
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=3)
                    data = json.loads(raw)
                    if data.get("event") == "task.dispatch":
                        tid = data["payload"]["task_id"]
                        await ws.send(json.dumps({"event": "task.completed", "payload": {"task_id": tid}}))
                        print(f"[smoke] acked task {tid}")
                        break
            except asyncio.TimeoutError:
                print("[smoke] !! no task.dispatch received", file=sys.stderr)
                return 1

            await asyncio.sleep(0.3)

            # 7. verify our human-sent message status = sent
            r = await http.get(f"/conversations/{conv['id']}/messages", headers=auth)
            msgs = r.json()
            mine = next((m for m in msgs if m["id"] == msg_id), None)
            assert mine and mine["status"] == "sent", f"expected sent, got {mine}"
            print("[smoke] outbound human message status=sent OK")

    print("[smoke] ALL PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
