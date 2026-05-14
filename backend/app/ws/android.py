from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, Query, WebSocket, WebSocketDisconnect
from sqlalchemy import select

from pathlib import Path

from app.core.db import SessionLocal
from app.core.ws_manager import hub
from app.models import Robot, RobotTask
from app.routers.robots import verify_robot_token
from app.schemas import AndroidMessageReceived
from app.services.conversation import ingest_inbound_message
from app.services.task_dispatcher import update_task_on_callback

log = logging.getLogger(__name__)
router = APIRouter()


async def _auth_robot(robot_id: str, token: str) -> Robot | None:
    async with SessionLocal() as db:
        robot = (
            await db.execute(select(Robot).where(Robot.robot_id == robot_id))
        ).scalar_one_or_none()
        if not robot:
            return None
        if not verify_robot_token(token, robot.token_hash):
            return None
        return robot


@router.websocket("/ws/android")
async def android_ws(
    ws: WebSocket, robot_id: str = Query(...), token: str = Query(...)
) -> None:
    robot = await _auth_robot(robot_id, token)
    if not robot:
        await ws.close(code=4401)
        return

    await ws.accept()
    await hub.connect_android(robot.robot_id, ws)

    async with SessionLocal() as db:
        r = await db.get(Robot, robot.id)
        r.status = "online"
        r.last_seen_at = datetime.now(timezone.utc)
        await db.commit()
    await hub.broadcast_web(
        robot.team_id, "robot.status", {"robot_id": robot.robot_id, "status": "online"}
    )

    # flush any pending tasks
    async with SessionLocal() as db:
        pending = (
            await db.execute(
                select(RobotTask)
                .where(RobotTask.robot_id == robot.id, RobotTask.status == "pending")
                .order_by(RobotTask.id)
            )
        ).scalars().all()
        for task in pending:
            sent = await hub.send_android(
                robot.robot_id,
                "task.dispatch",
                {"task_id": task.id, "type": task.type, "payload": task.payload_json},
            )
            if sent:
                task.status = "dispatched"
        await db.commit()

    try:
        while True:
            data = await ws.receive_json()
            await _handle_event(robot, data)
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.warning("android ws error: %s", e)
    finally:
        await hub.disconnect_android(robot.robot_id, ws)
        async with SessionLocal() as db:
            r = await db.get(Robot, robot.id)
            if r:
                r.status = "offline"
                await db.commit()
        await hub.broadcast_web(
            robot.team_id,
            "robot.status",
            {"robot_id": robot.robot_id, "status": "offline"},
        )


async def _handle_event(robot: Robot, data: dict) -> None:
    event = data.get("event")
    payload = data.get("payload") or {}

    if event == "device.hello" or event == "device.heartbeat":
        async with SessionLocal() as db:
            r = await db.get(Robot, robot.id)
            if r:
                r.last_seen_at = datetime.now(timezone.utc)
                if "current_page" in payload:
                    r.current_page = payload["current_page"]
                await db.commit()
        return

    if event == "message.received":
        evt = AndroidMessageReceived.model_validate(payload)
        async with SessionLocal() as db:
            r = await db.get(Robot, robot.id)
            if r:
                await ingest_inbound_message(db, r, evt)
        return

    if event in ("task.completed", "task.failed"):
        task_id = int(payload.get("task_id"))
        status = "completed" if event == "task.completed" else "failed"
        error = payload.get("error")
        async with SessionLocal() as db:
            r = await db.get(Robot, robot.id)
            if r:
                await update_task_on_callback(
                    db, robot=r, task_id=task_id, status=status, error=error
                )
        return

    if event == "device.ui_dump":
        _save_ui_dump(robot, payload)
        return

    log.info("unknown android event: %s", event)


def _save_ui_dump(robot: Robot, payload: dict) -> None:
    """Persist a UI tree dump under var/ui_dumps/ so we can calibrate locators."""
    reason = (payload.get("reason") or "manual").replace("/", "_")[:64]
    page = (payload.get("current_page") or "UNKNOWN")[:32]
    tree = payload.get("tree") or ""
    base = Path("var/ui_dumps")
    base.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fp = base / f"{robot.robot_id}-{ts}-{page}-{reason}.txt"
    fp.write_text(tree, encoding="utf-8")
    log.info("ui_dump saved: %s (%d bytes)", fp, len(tree))
