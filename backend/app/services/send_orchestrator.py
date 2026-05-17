"""Outgoing text orchestration.

The `robot_tasks` row is now an audit record for a backend-driven ReAct send,
not a command dispatched to Android. Android only receives typed
`device.command` primitives.
"""
from __future__ import annotations

import asyncio
import logging

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import SessionLocal
from app.core.ws_manager import hub
from app.device import DeviceClient
from app.models import Conversation, Message, Robot, RobotTask, RobotTaskLog
from app.models import utcnow
from app.schemas import MessageOut
from app.services import settings_service

log = logging.getLogger(__name__)

_REACT_PREFIX = "[react] "
_AUTO_REPLY_RETRY_DELAY_SEC = 5.0


# Per-device serialisation is the job of services.task_queue (one consumer
# per robot, priority-ordered). Producers don't need to hold any lock here.


async def create_and_dispatch_send_text(
    db: AsyncSession,
    *,
    robot: Robot,
    conv: Conversation,
    contact_external_id: str,
    text: str,
    sender_type: str,
    sender_id: int | None,
    feedback_message_ids: list[int] | None = None,
) -> tuple[Message, RobotTask]:
    if not settings.task_queue_enabled:
        raise RuntimeError("task queue disabled")
    msg = Message(
        conversation_id=conv.id,
        direction="out",
        sender_type=sender_type,
        sender_id=sender_id,
        type="text",
        content=text,
        status="pending",
    )
    db.add(msg)
    await db.flush()

    task = RobotTask(
        robot_id=robot.id,
        type="send_text",
        payload_json={
            "conversation_external_id": contact_external_id,
            "text": text,
            "feedback_message_ids": feedback_message_ids or [],
        },
        status="dispatched",
        conversation_id=conv.id,
        message_id=msg.id,
    )
    db.add(task)
    await db.flush()
    db.add(
        RobotTaskLog(
            robot_id=robot.id,
            task_id=task.id,
            level="info",
            message=f"send_text scheduled via ReAct contact={contact_external_id}",
        )
    )

    msg.task_id = task.id
    conv.last_message_at = msg.created_at
    conv.last_message_preview = text

    await db.commit()
    await db.refresh(msg)
    await db.refresh(task)

    # Hand off to the per-robot priority queue. Auto-replies sit at
    # PRIORITY_AUTO_REPLY — operator-typed agent goals jump ahead of them.
    from app.services import task_queue

    await task_queue.enqueue(
        robot.robot_id, "send_text", task.id, priority=task_queue.PRIORITY_AUTO_REPLY
    )
    await _broadcast_message_new(robot.team_id, conv.id, msg)
    return msg, task


async def create_and_dispatch_send_media(
    db: AsyncSession,
    *,
    robot: Robot,
    conv: Conversation,
    contact_external_id: str,
    kind: str,
    media: dict,
    caption: str,
    sender_type: str,
    sender_id: int | None,
    feedback_message_ids: list[int] | None = None,
) -> tuple[Message, RobotTask]:
    if not settings.task_queue_enabled:
        raise RuntimeError("task queue disabled")
    label = caption or str(media.get("filename") or ("图片" if kind == "image" else "视频"))
    msg = Message(
        conversation_id=conv.id,
        direction="out",
        sender_type=sender_type,
        sender_id=sender_id,
        type=kind,
        content=label,
        media_json=media,
        status="pending",
    )
    db.add(msg)
    await db.flush()

    task = RobotTask(
        robot_id=robot.id,
        type="send_media",
        payload_json={
            "conversation_external_id": contact_external_id,
            "kind": kind,
            "caption": caption,
            "media": {
                "url": media.get("url"),
                "mime": media.get("mime"),
                "filename": media.get("filename"),
                "bytes": media.get("bytes"),
            },
            "feedback_message_ids": feedback_message_ids or [],
        },
        status="dispatched",
        max_attempts=1,
        conversation_id=conv.id,
        message_id=msg.id,
    )
    db.add(task)
    await db.flush()
    db.add(
        RobotTaskLog(
            robot_id=robot.id,
            task_id=task.id,
            level="info",
            message=f"send_media scheduled via ReAct contact={contact_external_id} kind={kind}",
        )
    )

    msg.task_id = task.id
    conv.last_message_at = msg.created_at
    conv.last_message_preview = f"[{('图片' if kind == 'image' else '视频')}] {label}".strip()

    await db.commit()
    await db.refresh(msg)
    await db.refresh(task)

    from app.services import task_queue

    await task_queue.enqueue(
        robot.robot_id, "send_media", task.id, priority=task_queue.PRIORITY_AUTO_REPLY
    )
    await _broadcast_message_new(robot.team_id, conv.id, msg)
    return msg, task


async def append_task_log(
    db: AsyncSession,
    *,
    robot: Robot,
    task_id: int | None,
    level: str,
    message: str,
) -> None:
    safe_task_id: int | None = task_id
    if safe_task_id is not None:
        if safe_task_id <= 0:
            safe_task_id = None
        else:
            exists = await db.get(RobotTask, safe_task_id)
            if exists is None:
                safe_task_id = None
    row = RobotTaskLog(
        robot_id=robot.id, task_id=safe_task_id, level=level, message=message
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    await hub.broadcast_web(
        robot.team_id,
        "task.log",
        {
            "id": row.id,
            "robot_id": robot.robot_id,
            "task_id": task_id,
            "level": level,
            "message": message,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        },
    )


async def _broadcast_message_new(team_id: int, conv_id: int, msg: Message) -> None:
    await hub.broadcast_web(
        team_id,
        "message.new",
        {
            "conversation_id": conv_id,
            "message": MessageOut.model_validate(msg).model_dump(mode="json"),
        },
    )


async def _broadcast_message_update(team_id: int, msg: Message) -> None:
    await hub.broadcast_web(
        team_id,
        "message.updated",
        {
            "conversation_id": msg.conversation_id,
            "message": MessageOut.model_validate(msg).model_dump(mode="json"),
        },
    )


async def run_send_task(task_id: int) -> None:
    """Queue runner — invoked by services.task_queue once the device slot
    becomes available. Serialisation is the queue's job; this function just
    drives one task end-to-end."""
    from app.ai.react_agent import run_react

    async with SessionLocal() as db:
        try:
            task = await db.get(RobotTask, task_id)
            if task is None:
                return
            robot = await db.get(Robot, task.robot_id)
            if robot is None:
                return
            if await _skip_if_message_already_sent(db, robot, task):
                return

            goal = _goal_for_task(task)
            if goal is None:
                return

            async def _sink(level: str, message: str) -> None:
                async with SessionLocal() as inner:
                    await append_task_log(
                        inner,
                        robot=robot,
                        task_id=task.id,
                        level=level if level in ("info", "warn", "error") else "info",
                        message=message,
                    )

            log.info("react send start task=%s goal=%r", task.id, goal)
            device = DeviceClient(robot)
            session_started = False
            try:
                session_started = True
                ack = await device.react_session_start(timeout=4.0)
                if not ack.ok:
                    log.debug("react session start not acknowledged: %s", ack.message)
            except Exception:  # noqa: BLE001
                log.debug("react session start failed; continuing")
            try:
                await device.open_wecom(timeout=6.0)
                await asyncio.sleep(0.6)
            except Exception:  # noqa: BLE001
                log.debug("open_wecom pre-flight failed; continuing")

            ai_cfg = await settings_service.get(db, robot.team_id, "ai")
            force_llm = bool(
                ai_cfg.get("react_force_llm")
                if ai_cfg.get("react_force_llm") is not None
                else settings.react_force_llm
            )
            max_attempts = min(int(task.max_attempts or 1), settings.react_send_max_attempts)
            result = None
            media_error: str | None = None
            try:
                while True:
                    result = await run_react(
                        db,
                        robot,
                        goal,
                        max_steps=settings.react_text_max_steps,
                        step_timeout=settings.react_step_timeout_sec,
                        log_sink=_sink,
                        force_llm=force_llm,
                    )
                    if result.ok:
                        break
                    task = await db.get(RobotTask, task_id)
                    if task is None:
                        return
                    next_attempts = int(task.attempts or 0) + 1
                    if next_attempts >= max_attempts:
                        break
                    task.attempts = next_attempts
                    task.last_error = _REACT_PREFIX + result.summary
                    db.add(
                        RobotTaskLog(
                            robot_id=robot.id,
                            task_id=task.id,
                            level="warn",
                            message=f"{task.last_error}; 保持设备槽位并立即重试 {task.attempts}/{task.max_attempts}",
                        )
                    )
                    await db.commit()
                    await asyncio.sleep(_AUTO_REPLY_RETRY_DELAY_SEC)
                    try:
                        await device.open_wecom(timeout=6.0)
                        await asyncio.sleep(0.6)
                    except Exception:  # noqa: BLE001
                        log.debug("open_wecom retry pre-flight failed; continuing")
                if result.ok and task.type == "send_media":
                    # Three-phase media send:
                    #   A) ReAct opened the target chat (above).
                    #   B) `stage_media` drops the file into Pictures/WeComAgent/
                    #      so WeCom's gallery picker can see it.
                    #   C) A second ReAct loop walks the "+ → 图片 → 选最新 → 发送"
                    #      flow inside the open chat. LLM + UI tree + screenshot
                    #      drive it; LocatorStore caches successful nodes by role
                    #      (compose_plus / media_picker_entry / gallery_first_item
                    #      / gallery_send_button) so subsequent runs short-circuit.
                    payload = task.payload_json or {}
                    media_payload = payload.get("media") or {}
                    stage = await device.stage_media(
                        download_url=str(media_payload.get("url") or ""),
                        mime=str(media_payload.get("mime") or ""),
                        filename=str(media_payload.get("filename") or "media"),
                        timeout=45.0,
                    )
                    if not stage.ok:
                        media_error = stage.message or "媒体落盘失败"
                    else:
                        stage_data = stage.data or {}
                        staged_name = str(
                            stage_data.get("display_name")
                            or media_payload.get("filename")
                            or "media"
                        )
                        media_goal = (
                            f"在当前聊天页面通过附件面板，发送刚刚落入相册的 "
                            f"{staged_name}（位于 Pictures/WeComAgent/）。\n"
                            f"路径示意（具体节点请按当前 UI tree 顺序与截图判断，不要写死文字）：\n"
                            f"  1) 在聊天输入栏中找到展开附件面板的入口（常是输入框右侧"
                            f"或左侧的小图标，UI 树里通常是一组并排的 ImageView；"
                            f"如点错弹出表情/语音面板，下一步换同一行里其它图标）；\n"
                            f"  2) 在弹出的面板中找到进入相册的入口（通常是一组并排"
                            f"图标 + 文字的格子，按 UI 树顺序里其中一个 label 对应"
                            f"相册/图片）；\n"
                            f"  3) 进入相册网格后：网格里的格子按 UI 树顺序排列，"
                            f"靠前的可能是相机/扫描等带文字标签的快捷入口，要跳过；"
                            f"选 UI 树里第一个**没有文字 label、只是纯 ImageView** 的"
                            f"缩略图（那就是最新存入的真实照片）；\n"
                            f"  4) 选中后必须再点确认发送按钮（通常在屏幕右下角，"
                            f"未选图前为禁用态）；若先进入预览/编辑页，则按相同顺序"
                            f"在该页右下角点发送；\n"
                            f"  5) 当 UI tree 的 page 字段从 picker/UNKNOWN 切回 CHAT，"
                            f"即表示已发出，立即 done(success=true)。点击发送后如果仍停在 "
                            f"UNKNOWN/picker/预览页，通常是上传或发送中，等待/继续观察，"
                            f"不要 back，不要进入其它聊天，也不要重新选择图片。\n"
                            f"对每次有效点击，请在 args 里写明 `_locator_role`，"
                            f"取值参考系统提示中的媒体角色，便于后续缓存。"
                            f"文件名 {staged_name}。"
                        )
                        media_result = await run_react(
                            db,
                            robot,
                            media_goal,
                            max_steps=settings.react_media_max_steps,
                            step_timeout=settings.react_step_timeout_sec,
                            log_sink=_sink,
                            force_llm=force_llm,
                        )
                        if not media_result.ok:
                            media_error = media_result.summary or "媒体发送失败"
                        # Keep the original open-chat result around for the
                        # success branch (its steps go into the audit log);
                        # the media phase logs are captured via _sink.
            finally:
                if session_started:
                    try:
                        await asyncio.shield(device.react_session_end(timeout=4.0))
                    except Exception:  # noqa: BLE001
                        log.debug("react session end failed")
            assert result is not None
            if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                raise asyncio.CancelledError
            task = await db.get(RobotTask, task_id)
            if task is None:
                return
            retry_task_id: int | None = None

            task_ok = bool(result.ok and media_error is None)
            if task_ok:
                task.status = "completed"
                task.last_error = None
                await _mark_feedback_messages(db, task, "replied")
                if task.message_id:
                    msg = await db.get(Message, task.message_id)
                    if msg:
                        msg.status = "sent"
            else:
                error = _REACT_PREFIX + (media_error or result.summary)
                retry_task_id = await _handle_send_failure(db, robot, task, error)
            db.add(
                RobotTaskLog(
                    robot_id=robot.id,
                    task_id=task.id,
                    level="info" if task_ok else "warn",
                    message=f"[react] result ok={task_ok} steps={len(result.steps)} summary={media_error or result.summary}",
                )
            )
            await db.commit()
            if retry_task_id is not None:
                from app.services import task_queue

                await asyncio.sleep(_AUTO_REPLY_RETRY_DELAY_SEC)
                await task_queue.enqueue(
                    robot.robot_id,
                    task.type,
                    retry_task_id,
                    priority=task_queue.PRIORITY_BACKGROUND,
                )
            await hub.broadcast_web(
                robot.team_id,
                "task.updated",
                {"task_id": task.id, "status": task.status, "error": task.last_error},
            )
            if task.message_id:
                m = await db.get(Message, task.message_id)
                if m:
                    await _broadcast_message_update(robot.team_id, m)
            await _wake_auto_reply_if_pending(db, robot.id, task.conversation_id)
        except asyncio.CancelledError:
            task = await db.get(RobotTask, task_id)
            if task is not None:
                task.status = "cancelled"
                task.last_error = "任务执行已中断"
                await _mark_feedback_messages(db, task, "failed")
                if task.message_id:
                    msg = await db.get(Message, task.message_id)
                    if msg:
                        msg.status = "cancelled"
                db.add(
                    RobotTaskLog(
                        robot_id=robot.id,
                        task_id=task.id,
                        level="warn",
                        message="[react] cancelled by operator",
                    )
                )
                await db.commit()
                await hub.broadcast_web(
                    robot.team_id,
                    "task.updated",
                    {"task_id": task.id, "status": task.status, "error": task.last_error},
                )
                if task.message_id:
                    m = await db.get(Message, task.message_id)
                    if m:
                        await _broadcast_message_update(robot.team_id, m)
                await _wake_auto_reply_if_pending(db, robot.id, task.conversation_id)
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("react send crashed: %s", e)
            try:
                await db.rollback()
            except Exception:  # noqa: BLE001
                pass
            task = await db.get(RobotTask, task_id)
            if task is None:
                return
            error = _REACT_PREFIX + (str(e) or e.__class__.__name__)
            retry_task_id = await _handle_send_failure(db, robot, task, error)
            db.add(
                RobotTaskLog(
                    robot_id=robot.id,
                    task_id=task.id,
                    level="error",
                    message=f"[react] crashed: {error}",
                )
            )
            await db.commit()
            if retry_task_id is not None:
                from app.services import task_queue

                await asyncio.sleep(_AUTO_REPLY_RETRY_DELAY_SEC)
                await task_queue.enqueue(
                    robot.robot_id,
                    task.type,
                    retry_task_id,
                    priority=task_queue.PRIORITY_BACKGROUND,
                )
            await hub.broadcast_web(
                robot.team_id,
                "task.updated",
                {"task_id": task.id, "status": task.status, "error": task.last_error},
            )
            if task.message_id:
                m = await db.get(Message, task.message_id)
                if m:
                    await _broadcast_message_update(robot.team_id, m)
            await _wake_auto_reply_if_pending(db, robot.id, task.conversation_id)


def _goal_for_task(task: RobotTask) -> str | None:
    payload = task.payload_json or {}
    contact = payload.get("conversation_external_id") or "目标联系人"
    if task.type == "send_media":
        return f"打开与「{contact}」的聊天"
    if task.type != "send_text":
        return None
    text = (payload.get("text") or "").strip()
    if not text:
        return None
    return f"打开与「{contact}」的聊天，并发送下面这段文本：{text}"


async def _skip_if_message_already_sent(
    db: AsyncSession, robot: Robot, task: RobotTask
) -> bool:
    """Treat recovered tasks as done when their outbound message already sent.

    Startup recovery can re-queue a `dispatched` task after the device finished
    the send but before the backend persisted the final task status. The message
    status is the stronger idempotency signal because it is what the operator
    and auto-reply feedback care about.
    """
    if not task.message_id:
        return False
    msg = await db.get(Message, task.message_id)
    if msg is None or msg.status != "sent":
        return False

    task.status = "completed"
    task.last_error = None
    await _mark_feedback_messages(db, task, "replied")
    db.add(
        RobotTaskLog(
            robot_id=robot.id,
            task_id=task.id,
            level="info",
            message="[react] skipped: outbound message is already sent",
        )
    )
    await db.commit()
    await hub.broadcast_web(
        robot.team_id,
        "task.updated",
        {"task_id": task.id, "status": task.status, "error": task.last_error},
    )
    await _broadcast_message_update(robot.team_id, msg)
    return True


async def _mark_feedback_messages(db: AsyncSession, task: RobotTask, status: str) -> None:
    ids = (task.payload_json or {}).get("feedback_message_ids") or []
    if not ids:
        return
    rows = (
        await db.execute(
            select(Message).where(
                Message.id.in_(ids),
                Message.direction == "in",
                Message.sender_type == "customer",
            )
        )
    ).scalars().all()
    for msg in rows:
        if status == "failed" and msg.feedback_status == "replied":
            continue
        msg.feedback_status = status
        msg.feedback_at = utcnow()


async def _handle_send_failure(
    db: AsyncSession, robot: Robot, task: RobotTask, error: str
) -> int | None:
    task.attempts = int(task.attempts or 0) + 1
    task.last_error = error
    if task.attempts < min(int(task.max_attempts or 1), settings.react_send_max_attempts):
        task.status = "dispatched"
        await _mark_feedback_messages(db, task, "pending")
        if task.message_id:
            msg = await db.get(Message, task.message_id)
            if msg:
                msg.status = "pending"
        db.add(
            RobotTaskLog(
                robot_id=robot.id,
                task_id=task.id,
                level="warn",
                message=f"{error}; 将重试 {task.attempts}/{task.max_attempts}",
            )
        )
        return task.id

    task.status = "failed"
    await _mark_feedback_messages(db, task, "failed")
    if task.message_id:
        msg = await db.get(Message, task.message_id)
        if msg:
            msg.status = "failed"
    return None


async def _wake_auto_reply_if_pending(
    db: AsyncSession, robot_pk: int, conv_id: int | None
) -> None:
    if not settings.auto_reply_enabled:
        return
    if conv_id is None:
        return
    row = (
        await db.execute(
            select(Message.id)
            .where(
                Message.conversation_id == conv_id,
                Message.direction == "in",
                Message.sender_type == "customer",
                Message.feedback_status.in_(("pending", "processing")),
            )
            .limit(1)
        )
    ).first()
    if row is None:
        return
    from app.services import auto_reply_scheduler

    auto_reply_scheduler.wake_robot(robot_pk)
