"""Run the full backend against a real Postgres DB.

Skips politely if PG_TEST_URL isn't set, so CI without docker is fine.

Required env:
    PG_TEST_URL   e.g. postgresql+asyncpg://user:pwd@localhost:5432/wecom_ai_test
                  The DB must already exist (the smoke creates the schema).

What it does:
  1. Drop & recreate all tables on the target DB (uses alembic downgrade base
     + upgrade head)
  2. Start uvicorn against this DATABASE_URL
  3. Run the existing MVP1 + settings + ai smoke flows
  4. Tear down

If the DB has prior data, this test is destructive. Don't point it at prod.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx
import websockets

PG_URL = os.environ.get("PG_TEST_URL", "").strip()
BASE = "http://127.0.0.1:8101"           # different port so we don't collide
WS_BASE = "ws://127.0.0.1:8101"
BACKEND = Path(__file__).resolve().parent.parent / "backend"


def skip(reason: str) -> int:
    print(f"[pg-smoke] SKIP: {reason}", flush=True)
    return 0


def run_alembic(cmd: list[str]) -> None:
    env = {**os.environ, "DATABASE_URL": PG_URL}
    r = subprocess.run(
        [sys.executable, "-m", "alembic", *cmd],
        cwd=BACKEND, env=env, capture_output=True, text=True,
    )
    if r.returncode != 0:
        raise RuntimeError(f"alembic {' '.join(cmd)} failed:\n{r.stderr}\n{r.stdout}")


def start_backend() -> subprocess.Popen:
    env = {**os.environ, "DATABASE_URL": PG_URL, "LOG_LEVEL": "WARNING"}
    return subprocess.Popen(
        [
            sys.executable, "-m", "uvicorn", "app.main:app",
            "--host", "127.0.0.1", "--port", "8101", "--log-level", "warning",
        ],
        cwd=BACKEND,
        env=env,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


async def wait_ready(timeout: float = 15.0) -> None:
    async with httpx.AsyncClient(base_url=BASE, timeout=2) as http:
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                r = await http.get("/healthz")
                if r.status_code == 200:
                    return
            except Exception:
                pass
            await asyncio.sleep(0.4)
    raise RuntimeError("backend did not become ready")


async def main() -> int:
    if not PG_URL:
        return skip("PG_TEST_URL not set")
    if "+asyncpg" not in PG_URL:
        return skip("PG_TEST_URL must use postgresql+asyncpg://")
    print(f"[pg-smoke] target: {PG_URL.split('@')[-1]}")

    # 1. clean slate via alembic
    try:
        run_alembic(["downgrade", "base"])
    except RuntimeError as e:
        # tolerate "no such table" on a brand-new DB
        if "alembic_version" not in str(e) and "does not exist" not in str(e):
            raise
    run_alembic(["upgrade", "head"])
    print("[pg-smoke] schema applied via alembic")

    # 2. start backend pointing at Postgres
    proc = start_backend()
    try:
        await wait_ready()
        print("[pg-smoke] backend up on 8101")

        async with httpx.AsyncClient(base_url=BASE, timeout=10) as http:
            r = await http.post("/auth/login", json={"email": "admin@example.com", "password": "admin123"})
            r.raise_for_status()
            auth = {"Authorization": f"Bearer {r.json()['access_token']}"}
            print("[pg-smoke] login OK")

            r = await http.post("/robots", headers=auth, json={"name": "pg-bot"})
            r.raise_for_status()
            d = r.json()
            rid, rtoken = d["robot"]["robot_id"], d["token"]

            url = f"{WS_BASE}/ws/android?robot_id={rid}&token={rtoken}"
            async with websockets.connect(url) as ws:
                await ws.send(json.dumps({"event": "device.hello", "payload": {"current_page": "HOME"}}))
                await ws.send(json.dumps({
                    "event": "message.received",
                    "payload": {
                        "contact": {"external_id": "wxid_pg", "nickname": "PG客户"},
                        "external_msg_id": f"pg_{uuid.uuid4().hex[:8]}",
                        "type": "text",
                        "content": "你好,我从 Postgres 来",
                    },
                }))
                # ack any AI dispatch (mode defaults to mixed → mock LLM auto-replies)
                acked = 0
                try:
                    while True:
                        raw = await asyncio.wait_for(ws.recv(), timeout=3.0)
                        data = json.loads(raw)
                        if data.get("event") == "task.dispatch":
                            await ws.send(json.dumps({"event": "task.completed", "payload": {"task_id": data["payload"]["task_id"]}}))
                            acked += 1
                except asyncio.TimeoutError:
                    pass
            print(f"[pg-smoke] AI auto-reply acked {acked}")

            r = await http.get("/conversations", headers=auth)
            r.raise_for_status()
            convs = r.json()
            assert convs, "no conversation persisted"
            preview = convs[0]["last_message_preview"]
            assert preview, "preview missing"
            print(f"[pg-smoke] conversation preview: {preview!r}")

            # KB happy path on Postgres (also exercises JSON columns)
            r = await http.post("/kb", headers=auth, json={"name": "pg-kb", "description": "test"})
            r.raise_for_status()
            kb_id = r.json()["id"]
            r = await http.post(
                f"/kb/{kb_id}/docs/paste",
                headers=auth,
                data={"name": "n.md", "content": "我们的旗舰产品 ProMax 售价 ¥2999 元"},
            )
            r.raise_for_status()
            for _ in range(20):
                await asyncio.sleep(0.3)
                d = (await http.get(f"/kb/{kb_id}/docs/{r.json()['id']}", headers=auth)).json()
                if d["status"] in ("ready", "failed"):
                    break
            assert d["status"] == "ready", f"doc not ready: {d}"
            print(f"[pg-smoke] kb doc ready chunks={d['chunk_count']}")

            # settings round-trip (exercises team_settings JSON column)
            r = await http.put("/settings/retrieval", headers=auth, json={"top_k": 7, "min_score": 0.1})
            r.raise_for_status()
            cur = (await http.get("/settings", headers=auth)).json()
            assert cur["retrieval"]["top_k"] == 7
            print("[pg-smoke] team_settings JSON round-trip OK")

    finally:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()

    print("[pg-smoke] ALL PASSED ✓")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
