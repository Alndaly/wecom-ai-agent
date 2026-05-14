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

        # ---- regression: client-supplied sent_at must not control ordering ----
        # Send a "future" inbound, then a human reply; the AI/human reply's
        # created_at must still be >= the inbound's (server time wins).
        await test_clock_skew_ordering(http, auth)

        # ---- regression: POST /conversations/{id}/read clears unread_count ----
        await test_mark_read(http, auth)

        # ---- regression: WeCom "[N条]" aggregation prefix gets stripped ----
        await test_aggregation_prefix_strip(http, auth)

    print("[smoke] ALL PASSED ✓")
    return 0


async def test_aggregation_prefix_strip(http, auth):
    # Fresh robot, mode=human so AI doesn't muddy the message stream
    r = await http.post("/robots", headers=auth, json={"name": "prefix-bot"})
    r.raise_for_status()
    d = r.json()
    rid, rtoken = d["robot"]["robot_id"], d["token"]

    cases = [
        ("111", "111"),
        ("[2条]222", "222"),
        ("[ 3 条 ] 333", "333"),                      # whitespace tolerated
        ("[10条]hello world", "hello world"),
        ("不是前缀[2条]的消息", "不是前缀[2条]的消息"),   # only strips at the start
    ]

    url = f"{WS_BASE}/ws/android?robot_id={rid}&token={rtoken}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"event": "device.hello", "payload": {"current_page": "HOME"}}))
        for raw, _ in cases:
            await ws.send(json.dumps({
                "event": "message.received",
                "payload": {
                    "contact": {"external_id": "wxid_prefix", "nickname": "前缀客户"},
                    "external_msg_id": f"pfx_{uuid.uuid4().hex[:8]}",
                    "type": "text",
                    "content": raw,
                },
            }))
            # small gap so timestamps are strictly increasing
            await asyncio.sleep(0.05)
        await asyncio.sleep(0.4)

    convs = (
        await http.get("/conversations", headers=auth, params={"robot_id": d["robot"]["id"]})
    ).json()
    assert convs, "no conv for prefix-bot"
    await http.patch(f"/conversations/{convs[0]['id']}", headers=auth, json={"mode": "human"})

    msgs = (
        await http.get(f"/conversations/{convs[0]['id']}/messages?limit=200", headers=auth)
    ).json()
    inbound = [m for m in msgs if m["direction"] == "in"]
    actual = [m["content"] for m in inbound]
    expected = [c for _, c in cases]
    assert actual == expected, f"expected {expected}, got {actual}"
    print(f"[smoke] [N条] aggregation prefix stripped OK ({len(actual)} cases)")


async def test_mark_read(http, auth):
    # produce an unread inbound on a fresh robot (mode forced to human so AI
    # doesn't auto-reply and skew counters)
    r = await http.post("/robots", headers=auth, json={"name": "read-bot"})
    r.raise_for_status()
    d = r.json()
    rid, rtoken = d["robot"]["robot_id"], d["token"]

    url = f"{WS_BASE}/ws/android?robot_id={rid}&token={rtoken}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"event": "device.hello", "payload": {"current_page": "HOME"}}))
        await ws.send(json.dumps({
            "event": "message.received",
            "payload": {
                "contact": {"external_id": "wxid_read", "nickname": "Read客户"},
                "external_msg_id": f"rd_{uuid.uuid4().hex[:8]}",
                "type": "text",
                "content": "hello unread",
            },
        }))
        await asyncio.sleep(0.4)

    convs = (
        await http.get("/conversations", headers=auth, params={"robot_id": d["robot"]["id"]})
    ).json()
    assert convs, "no conv for read-bot"
    target = convs[0]
    # if AI auto-replied, the inbound is still 1; we don't care which mode here
    assert target["unread_count"] > 0, f"expected unread > 0; got {target}"

    r = await http.post(f"/conversations/{target['id']}/read", headers=auth)
    r.raise_for_status()
    after = r.json()
    assert after["unread_count"] == 0, f"unread not cleared: {after}"

    # idempotent: second call still 0
    r2 = await http.post(f"/conversations/{target['id']}/read", headers=auth)
    r2.raise_for_status()
    assert r2.json()["unread_count"] == 0
    print("[smoke] mark-read clears unread_count OK (idempotent)")


async def test_clock_skew_ordering(http, auth):
    from datetime import timedelta
    # create a fresh robot just for this
    r = await http.post("/robots", headers=auth, json={"name": "skew-bot"})
    r.raise_for_status()
    d = r.json()
    rid, rtoken = d["robot"]["robot_id"], d["token"]

    # force mode=human first to avoid AI auto-reply complicating timestamps
    url = f"{WS_BASE}/ws/android?robot_id={rid}&token={rtoken}"
    async with websockets.connect(url) as ws:
        await ws.send(json.dumps({"event": "device.hello", "payload": {"current_page": "HOME"}}))

        future_ts = (datetime.now(timezone.utc) + timedelta(seconds=120)).isoformat()
        await ws.send(json.dumps({
            "event": "message.received",
            "payload": {
                "contact": {"external_id": "wxid_skew", "nickname": "未来客户"},
                "external_msg_id": f"skew_{uuid.uuid4().hex[:8]}",
                "type": "text",
                "content": "我从未来发来的消息",
                "sent_at": future_ts,
            },
        }))
        await asyncio.sleep(0.3)

    convs = (await http.get("/conversations", headers=auth, params={"robot_id": d["robot"]["id"]})).json()
    conv = convs[0]
    await http.patch(f"/conversations/{conv['id']}", headers=auth, json={"mode": "human"})
    # send a human reply
    await http.post(
        f"/conversations/{conv['id']}/messages",
        headers=auth,
        json={"type": "text", "content": "我从现在回复"},
    )

    msgs = (await http.get(f"/conversations/{conv['id']}/messages", headers=auth)).json()
    msgs.sort(key=lambda m: m["id"])  # insertion order
    inbound = [m for m in msgs if m["direction"] == "in"][0]
    outbound = [m for m in msgs if m["direction"] == "out"][0]

    def parse(ts: str):
        s = ts.replace("Z", "+00:00")
        d = datetime.fromisoformat(s)
        if d.tzinfo is None:
            d = d.replace(tzinfo=timezone.utc)
        return d

    in_ts = parse(inbound["created_at"])
    out_ts = parse(outbound["created_at"])
    assert out_ts >= in_ts, (
        f"server time ordering broken: out({out_ts}) < in({in_ts}); "
        f"inbound created_at is from client clock"
    )
    # and definitely not the +120s future from the wire
    delta = (datetime.now(timezone.utc) - in_ts).total_seconds()
    assert abs(delta) < 30, f"inbound timestamp not from server clock; delta={delta}s"
    print("[smoke] clock-skew ordering OK (server time used)")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
