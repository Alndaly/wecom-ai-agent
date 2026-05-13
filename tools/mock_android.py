"""Mock Android client.

Connects to backend /ws/android as a robot, can:
  - send a fake `message.received` event
  - auto-ack any `task.dispatch` it receives (as `task.completed`)

Usage:
    python tools/mock_android.py \
        --base ws://localhost:8000 --robot-id robot_xxxx --token <robot_token> \
        [--send "你好"] [--from-id wxid_test --from-name 张三]

Without --send the client just stays online and acks dispatched tasks.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import uuid
from datetime import datetime, timezone

import websockets


async def main(args: argparse.Namespace) -> None:
    url = f"{args.base}/ws/android?robot_id={args.robot_id}&token={args.token}"
    async with websockets.connect(url) as ws:
        print(f"[mock] connected as {args.robot_id}")

        async def send(event: str, payload: dict) -> None:
            await ws.send(json.dumps({"event": event, "payload": payload}))

        await send("device.hello", {"version": "mock-0.1", "current_page": "HOME"})

        async def heartbeat() -> None:
            while True:
                await asyncio.sleep(30)
                try:
                    await send("device.heartbeat", {"current_page": "HOME"})
                except Exception:
                    return

        hb_task = asyncio.create_task(heartbeat())

        if args.send:
            await asyncio.sleep(0.3)
            await send(
                "message.received",
                {
                    "contact": {"external_id": args.from_id, "nickname": args.from_name},
                    "external_msg_id": f"mock_{uuid.uuid4().hex[:8]}",
                    "type": "text",
                    "content": args.send,
                    "sent_at": datetime.now(timezone.utc).isoformat(),
                },
            )
            print(f"[mock] sent inbound message: {args.send!r}")

        try:
            while True:
                raw = await ws.recv()
                data = json.loads(raw)
                event = data.get("event")
                payload = data.get("payload") or {}
                print(f"[mock] <- {event} {payload}")
                if event == "task.dispatch":
                    await asyncio.sleep(0.5)
                    await send("task.completed", {"task_id": payload["task_id"]})
                    print(f"[mock] -> task.completed task_id={payload['task_id']}")
        finally:
            hb_task.cancel()


if __name__ == "__main__":
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="ws://localhost:8000")
    p.add_argument("--robot-id", required=True)
    p.add_argument("--token", required=True)
    p.add_argument("--send", default=None, help="inbound message text to send")
    p.add_argument("--from-id", default="wxid_test_001")
    p.add_argument("--from-name", default="测试客户")
    args = p.parse_args()
    asyncio.run(main(args))
