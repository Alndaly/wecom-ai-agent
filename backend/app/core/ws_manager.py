"""In-memory WebSocket fan-out.

MVP1: single-process. For multi-worker deployments, replace with Redis pub/sub
(see ADR-0003).
"""
from __future__ import annotations

import asyncio
import uuid
from collections import defaultdict
from typing import Any

from fastapi import WebSocket


class WsHub:
    def __init__(self) -> None:
        # team_id -> set of web sockets
        self._web: dict[int, set[WebSocket]] = defaultdict(set)
        # robot_id (string) -> single Android socket
        self._android: dict[str, WebSocket] = {}
        # request_id -> Future, used by send_request / resolve_request
        self._pending: dict[str, asyncio.Future[dict[str, Any]]] = {}
        self._lock = asyncio.Lock()

    # ---------- web ----------
    async def connect_web(self, team_id: int, ws: WebSocket) -> None:
        async with self._lock:
            self._web[team_id].add(ws)

    async def disconnect_web(self, team_id: int, ws: WebSocket) -> None:
        async with self._lock:
            self._web[team_id].discard(ws)

    async def broadcast_web(self, team_id: int, event: str, payload: dict[str, Any]) -> None:
        msg = {"event": event, "payload": payload}
        dead: list[WebSocket] = []
        for ws in list(self._web.get(team_id, ())):
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        if dead:
            async with self._lock:
                for ws in dead:
                    self._web[team_id].discard(ws)

    # ---------- android ----------
    async def connect_android(self, robot_id: str, ws: WebSocket) -> None:
        async with self._lock:
            # kick old connection if any
            old = self._android.get(robot_id)
            if old is not None:
                try:
                    await old.close()
                except Exception:
                    pass
            self._android[robot_id] = ws

    async def disconnect_android(self, robot_id: str, ws: WebSocket) -> None:
        async with self._lock:
            if self._android.get(robot_id) is ws:
                self._android.pop(robot_id, None)

    def is_android_online(self, robot_id: str) -> bool:
        return robot_id in self._android

    async def send_android(self, robot_id: str, event: str, payload: dict[str, Any]) -> bool:
        ws = self._android.get(robot_id)
        if ws is None:
            return False
        try:
            await ws.send_json({"event": event, "payload": payload})
            return True
        except Exception:
            await self.disconnect_android(robot_id, ws)
            return False

    # ---------- request/response (correlated by request_id) ----------
    # Used by the ReAct agent to send a `device.command` and await a matching
    # ack/result. The Android side echoes `request_id` back inside its event
    # payload; `resolve_request` is called by the WS event handler.
    async def send_request(
        self,
        robot_id: str,
        event: str,
        payload: dict[str, Any],
        *,
        timeout: float = 15.0,
    ) -> dict[str, Any]:
        request_id = uuid.uuid4().hex
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_event_loop().create_future()
        self._pending[request_id] = fut
        body = {**payload, "request_id": request_id}
        ok = await self.send_android(robot_id, event, body)
        if not ok:
            self._pending.pop(request_id, None)
            raise RuntimeError("robot offline")
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError as e:
            raise TimeoutError(f"no response from device for {timeout}s") from e
        finally:
            self._pending.pop(request_id, None)

    def resolve_request(self, request_id: str | None, value: dict[str, Any]) -> bool:
        if not request_id:
            return False
        fut = self._pending.get(request_id)
        if fut is None or fut.done():
            return False
        fut.set_result(value)
        return True


hub = WsHub()
