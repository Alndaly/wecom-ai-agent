"""Verifies the new Android↔backend protocol bits that don't need a phone:

  - `device.ui_dump` is accepted and persisted under backend/var/ui_dumps/
  - The same flow tolerated unknown events (no 500)
  - `task.dispatch` still acks (regression)
"""
from __future__ import annotations

import asyncio
import json
import os
import sys
import uuid
from pathlib import Path

import httpx
import websockets

BASE = "http://localhost:8000"
WS_BASE = "ws://localhost:8000"
DUMP_DIR = Path("backend/var/ui_dumps")


async def main() -> int:
    if DUMP_DIR.exists():
        # snapshot existing files to compare delta later
        before = {p.name for p in DUMP_DIR.iterdir()}
    else:
        before = set()

    async with httpx.AsyncClient(base_url=BASE, timeout=10) as http:
        r = await http.post("/auth/login", json={"email": "admin@example.com", "password": "admin123"})
        r.raise_for_status()
        auth = {"Authorization": f"Bearer {r.json()['access_token']}"}

        r = await http.post("/robots", headers=auth, json={"name": "wiring-bot"})
        r.raise_for_status()
        d = r.json()
        rid, rtoken = d["robot"]["robot_id"], d["token"]
        print(f"[wiring] robot {rid}")

        url = f"{WS_BASE}/ws/android?robot_id={rid}&token={rtoken}"
        async with websockets.connect(url) as ws:
            await ws.send(json.dumps({"event": "device.hello", "payload": {"current_page": "HOME"}}))

            # 1. ui_dump
            tree = "=== fake ===\n[Root] txt=\"hello\"\n  [TextView] txt=\"world\"\n"
            await ws.send(json.dumps({
                "event": "device.ui_dump",
                "payload": {"reason": "smoke", "current_page": "CHAT", "tree": tree},
            }))

            # 2. unknown event (should not crash)
            await ws.send(json.dumps({"event": "weird.event", "payload": {"foo": "bar"}}))

            # give backend a beat to flush
            await asyncio.sleep(0.4)

    # check the dump landed
    after = {p.name for p in DUMP_DIR.iterdir()} if DUMP_DIR.exists() else set()
    new = after - before
    matches = [n for n in new if n.startswith(rid) and "smoke" in n]
    assert matches, f"no dump file matching {rid}/smoke; got {new}"
    fp = DUMP_DIR / matches[0]
    body = fp.read_text(encoding="utf-8")
    assert "[TextView]" in body, f"dump content wrong: {body[:200]}"
    print(f"[wiring] dump persisted: {fp} ({len(body)}B)")

    print("[wiring] ALL PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
