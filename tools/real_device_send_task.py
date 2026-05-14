"""Smoke-test the real device outbound send chain.

Flow:
  1. Login as the web user.
  2. Pick a robot and an existing conversation.
  3. POST /conversations/{cid}/messages to create a send_text RobotTask.
  4. Listen on /ws/web until the backend reports task.completed/failed and
     message.updated.

Prerequisite: create the conversation naturally first, e.g. have the target
contact send one message to the WeCom account so NotificationListener /
AccessibilityService ingests it.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time
from typing import Any

import httpx
import websockets


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser()
    p.add_argument("--base", default="http://localhost:8000")
    p.add_argument("--ws-base", default="ws://localhost:8000")
    p.add_argument("--email", default="admin@example.com")
    p.add_argument("--password", default="admin123")
    p.add_argument("--robot", help="robot DB id or robot_id; defaults to first online robot")
    p.add_argument("--conversation-id", type=int, help="conversation id; defaults to newest for robot")
    p.add_argument("--text", default=f"真机链路测试 {int(time.time())}")
    p.add_argument("--timeout", type=float, default=45.0)
    return p.parse_args()


async def login(http: httpx.AsyncClient, email: str, password: str) -> str:
    r = await http.post("/auth/login", json={"email": email, "password": password})
    r.raise_for_status()
    return r.json()["access_token"]


async def pick_robot(http: httpx.AsyncClient, auth: dict[str, str], robot_arg: str | None) -> dict[str, Any]:
    robots = (await http.get("/robots", headers=auth)).json()
    if not robots:
        raise RuntimeError("no robots; create one in Web /devices first")
    if robot_arg:
        for robot in robots:
            if str(robot["id"]) == robot_arg or robot["robot_id"] == robot_arg:
                return robot
        raise RuntimeError(f"robot not found: {robot_arg}")
    online = [r for r in robots if r.get("status") == "online"]
    if not online:
        names = ", ".join(f"{r['id']}:{r['robot_id']}:{r['status']}" for r in robots)
        raise RuntimeError(f"no online robot; got {names}")
    return online[0]


async def pick_conversation(
    http: httpx.AsyncClient,
    auth: dict[str, str],
    robot: dict[str, Any],
    conversation_id: int | None,
) -> dict[str, Any]:
    if conversation_id is not None:
        r = await http.get(f"/conversations/{conversation_id}", headers=auth)
        r.raise_for_status()
        return r.json()
    convs = (await http.get(f"/conversations?robot_id={robot['id']}", headers=auth)).json()
    if not convs:
        raise RuntimeError(
            "no conversation for this robot; send one inbound message to WeCom first "
            "so the backend creates a contact/conversation"
        )
    return convs[0]


async def wait_for_result(ws_url: str, token: str, task_id: int, message_id: int, timeout: float) -> tuple[str, str | None]:
    deadline = time.monotonic() + timeout
    async with websockets.connect(f"{ws_url}/ws/web?token={token}") as ws:
        while time.monotonic() < deadline:
            remaining = max(0.1, deadline - time.monotonic())
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
            data = json.loads(raw)
            event = data.get("event")
            payload = data.get("payload") or {}
            if event == "task.updated" and payload.get("task_id") == task_id:
                status = payload.get("status")
                error = payload.get("error")
                print(f"[real-send] task.updated status={status} error={error}")
                if status in ("completed", "failed", "timeout"):
                    return status, error
            if event == "message.updated":
                msg = payload.get("message") or {}
                if msg.get("id") == message_id:
                    print(f"[real-send] message.updated status={msg.get('status')}")
    raise TimeoutError(f"timed out waiting for task {task_id}")


async def main() -> int:
    args = parse_args()
    async with httpx.AsyncClient(base_url=args.base, timeout=20) as http:
        token = await login(http, args.email, args.password)
        auth = {"Authorization": f"Bearer {token}"}

        robot = await pick_robot(http, auth, args.robot)
        print(f"[real-send] robot id={robot['id']} robot_id={robot['robot_id']} status={robot['status']}")

        conv = await pick_conversation(http, auth, robot, args.conversation_id)
        contact = conv["contact"]["nickname"] or conv["contact"]["external_id"]
        print(f"[real-send] conversation id={conv['id']} contact={contact!r} mode={conv['mode']}")

        r = await http.post(
            f"/conversations/{conv['id']}/messages",
            headers=auth,
            json={"type": "text", "content": args.text},
        )
        r.raise_for_status()
        body = r.json()
        msg = body["message"]
        task = body["task"]
        print(f"[real-send] created message={msg['id']} task={task['id']} initial_task_status={task['status']}")

        status, error = await wait_for_result(args.ws_base, token, task["id"], msg["id"], args.timeout)
        if status != "completed":
            raise RuntimeError(f"send failed: task status={status} error={error}")
        print("[real-send] ALL PASSED - real device completed send task")
        return 0


if __name__ == "__main__":
    try:
        raise SystemExit(asyncio.run(main()))
    except Exception as e:
        print(f"[real-send] FAILED: {e}", file=sys.stderr)
        raise SystemExit(1)
