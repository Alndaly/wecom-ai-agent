from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile, WebSocket, WebSocketDisconnect, status
from sqlalchemy import select

from pathlib import Path

from app.core.db import SessionLocal
from app.core.ws_manager import hub
from app.models import Robot
from app.routers.robots import verify_robot_token
from app.schemas import AndroidMessageReceived
from app.services.media_store import persist_upload_bytes
from app.services.conversation import ingest_inbound_message
from app.services.send_orchestrator import append_task_log

log = logging.getLogger(__name__)
router = APIRouter()


@router.post("/android/inbound-media")
async def upload_inbound_media(
    robot_id: str = Form(...),
    token: str = Form(...),
    type: str = Form(...),
    file: UploadFile = File(...),
) -> dict:
    robot = await _auth_robot(robot_id, token)
    if not robot:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid robot credentials")
    if type not in {"image", "video"}:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "unsupported media type")
    raw = await file.read()
    media = await persist_upload_bytes(
        raw,
        team_id=robot.team_id,
        kind=type,
        mime=file.content_type or "",
        filename=file.filename or "inbound-media",
    )
    return {"media": media}


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
    log.info("android ws connected robot=%s team=%s", robot.robot_id, robot.team_id)

    async with SessionLocal() as db:
        r = await db.get(Robot, robot.id)
        r.status = "online"
        r.last_seen_at = datetime.now(timezone.utc)
        await db.commit()
    await hub.broadcast_web(
        robot.team_id, "robot.status", {"robot_id": robot.robot_id, "status": "online"}
    )

    try:
        while True:
            data = await ws.receive_json()
            await _handle_event(robot, data)
    except WebSocketDisconnect as e:
        log.info(
            "android ws disconnect event robot=%s code=%s reason=%s",
            robot.robot_id,
            getattr(e, "code", None),
            getattr(e, "reason", None),
        )
    except Exception as e:
        log.warning("android ws error: %s", e)
    finally:
        await hub.disconnect_android(robot.robot_id, ws)
        log.info("android ws disconnected robot=%s", robot.robot_id)
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
                _apply_device_status(r, payload)
                await db.commit()
        await hub.broadcast_web(
            robot.team_id,
            "robot.updated",
            _robot_payload(robot.robot_id, payload),
        )
        return

    if event == "message.received":
        evt = AndroidMessageReceived.model_validate(payload)
        log.info(
            "[message-callback] ws received robot=%s contact=%s sender_type=%s "
            "external_msg_id=%s content=%r",
            robot.robot_id,
            evt.contact.external_id,
            evt.sender_type,
            evt.external_msg_id,
            evt.content or "",
        )
        async with SessionLocal() as db:
            r = await db.get(Robot, robot.id)
            if r:
                msg = await ingest_inbound_message(db, r, evt)
                if msg is None:
                    log.debug(
                        "android message skipped robot=%s contact=%s sender_type=%s content=%r",
                        robot.robot_id,
                        evt.contact.external_id,
                        evt.sender_type,
                        evt.content or "",
                    )
                else:
                    log.info(
                        "android message accepted robot=%s contact=%s sender_type=%s direction=%s content=%r",
                        robot.robot_id,
                        evt.contact.external_id,
                        evt.sender_type,
                        msg.direction,
                        msg.content or "",
                    )
        return

    if event == "task.log":
        raw = payload.get("task_id")
        task_id = int(raw) if raw is not None else None
        # Same sentinel — drop the FK reference to NULL so PG doesn't blow up.
        if task_id is not None and task_id <= 0:
            task_id = None
        message = payload.get("message") or ""
        level = payload.get("level") or "info"
        async with SessionLocal() as db:
            r = await db.get(Robot, robot.id)
            if r:
                await append_task_log(db, robot=r, task_id=task_id, level=level, message=message)
        return

    if event == "device.ui_dump":
        dump = _save_ui_dump(robot, payload)
        await hub.broadcast_web(robot.team_id, "device.ui_dump", dump)
        # If this dump was the response to a ReAct agent request, deliver it.
        resolved = hub.resolve_request(payload.get("request_id"), dump)
        log.info(
            "device.ui_dump robot=%s request=%s resolved=%s nodes=%d",
            robot.robot_id,
            payload.get("request_id"),
            resolved,
            len(dump.get("nodes") or []),
        )
        return

    if event == "device.command_result":
        # Generic ack-with-result channel used by the ReAct agent. The device
        # echoes `request_id` so we can correlate. Payload also carries
        # `command`, `ok`, `message`, and an optional `data` object.
        resolved = hub.resolve_request(payload.get("request_id"), payload)
        log.info(
            "device.command_result robot=%s command=%s ok=%s request=%s resolved=%s msg=%r",
            robot.robot_id,
            payload.get("command"),
            payload.get("ok"),
            payload.get("request_id"),
            resolved,
            payload.get("message"),
        )
        await hub.broadcast_web(robot.team_id, "device.command_result", {
            "robot_id": robot.robot_id,
            **{k: v for k, v in payload.items() if k != "request_id"},
        })
        return

    if event == "device.screen_frame":
        await hub.broadcast_web(
            robot.team_id,
            "device.screen_frame",
            {
                "robot_id": robot.robot_id,
                "image": payload.get("image"),
                "mime": payload.get("mime") or "image/jpeg",
                "width": payload.get("width"),
                "height": payload.get("height"),
                "error": payload.get("error"),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return

    if event == "device.command_ack":
        await hub.broadcast_web(
            robot.team_id,
            "device.command_ack",
            {
                "robot_id": robot.robot_id,
                "command": payload.get("command"),
                "ok": payload.get("ok"),
                "message": payload.get("message"),
                "created_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        return

    log.info("unknown android event: %s", event)


def _apply_device_status(robot: Robot, payload: dict) -> None:
    fields = (
        "current_page",
        "device_type",
        "device_name",
        "manufacturer",
        "model",
        "android_version",
        "sdk_int",
        "app_version",
        "screen_width",
        "screen_height",
    )
    for field in fields:
        if field in payload:
            setattr(robot, field, payload[field])


def _robot_payload(robot_id: str, payload: dict) -> dict:
    return {
        "robot_id": robot_id,
        **{k: v for k, v in payload.items() if k != "battery"},
    }


def _save_ui_dump(robot: Robot, payload: dict) -> dict:
    """Persist a UI tree dump under var/ui_dumps/ so we can calibrate locators."""
    request_id = payload.get("request_id")
    reason = (payload.get("reason") or "manual").replace("/", "_")
    page = payload.get("current_page") or "UNKNOWN"
    tree = payload.get("tree") or ""
    nodes = payload.get("nodes") or []
    base = Path("var/ui_dumps")
    base.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    fp = base / f"{robot.robot_id}-{ts}-{page}-{reason}.txt"
    fp.write_text(tree, encoding="utf-8")
    log.info("ui_dump saved: %s (%d bytes, %d nodes)", fp, len(tree), len(nodes))
    return {
        "request_id": request_id,
        "robot_id": robot.robot_id,
        "current_page": page,
        "reason": reason,
        "tree": tree,
        "nodes": nodes,
        "screen_width": payload.get("screen_width"),
        "screen_height": payload.get("screen_height"),
        "input_panel_visible": payload.get("input_panel_visible"),
        "path": str(fp),
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
