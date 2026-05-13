from __future__ import annotations

import logging

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect

from app.core.db import SessionLocal
from app.core.security import decode_token
from app.core.ws_manager import hub
from app.models import User

log = logging.getLogger(__name__)
router = APIRouter()


@router.websocket("/ws/web")
async def web_ws(ws: WebSocket, token: str = Query(...)) -> None:
    try:
        payload = decode_token(token)
        uid = int(payload["sub"])
    except Exception:
        await ws.close(code=4401)
        return

    async with SessionLocal() as db:
        user = await db.get(User, uid)
    if not user:
        await ws.close(code=4401)
        return

    await ws.accept()
    await hub.connect_web(user.team_id, ws)
    try:
        while True:
            data = await ws.receive_json()
            if data.get("op") == "ping":
                await ws.send_json({"op": "pong"})
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("web ws error: %s", e)
    finally:
        await hub.disconnect_web(user.team_id, ws)
