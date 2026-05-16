import hashlib
import hmac
import secrets

import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import delete as sa_delete, select, update as sa_update
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import SessionLocal, get_db
from app.core.security import new_robot_token
from app.core.ws_manager import hub
from app.deps import current_user
from app.models import (
    AIReplyLog,
    Contact,
    Conversation,
    Message,
    Robot,
    RobotTask,
    RobotTaskLog,
    User,
    UserMemory,
    UserProfile,
)
from app.services import settings_service
from app.schemas import (
    AgentRunIn,
    AgentRunOut,
    RobotCommandOut,
    RobotCreateIn,
    RobotCreateOut,
    RobotOut,
    RobotTaskLogOut,
    RobotUiDumpRequestOut,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/robots", tags=["robots"])


# Robot tokens are 32-byte URL-safe random strings (~256 bits of entropy).
# We don't need bcrypt / argon2's slow KDF here — a brute-force search
# against the hash is infeasible regardless. HMAC-SHA256 keyed by the
# server's JWT secret gives us "DB leak ≠ token leak" without the
# CPU / library-compatibility cost of passlib.
def _hash_token(token: str) -> str:
    key = settings.jwt_secret.encode("utf-8")
    return hmac.new(key, token.encode("utf-8"), hashlib.sha256).hexdigest()


def verify_robot_token(token: str, hashed: str) -> bool:
    expected = _hash_token(token)
    return hmac.compare_digest(expected, hashed)


def _require_online(robot: Robot) -> None:
    if not hub.is_android_online(robot.robot_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "robot is offline")


async def _send_robot_command(robot: Robot, payload: dict) -> None:
    _require_online(robot)
    dispatched = await hub.send_android(robot.robot_id, "device.command", payload)
    if not dispatched:
        raise HTTPException(status.HTTP_409_CONFLICT, "robot is offline")


@router.get("", response_model=list[RobotOut])
async def list_robots(
    user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> list[Robot]:
    rows = (
        await db.execute(select(Robot).where(Robot.team_id == user.team_id).order_by(Robot.id))
    ).scalars().all()
    return list(rows)


@router.post("", response_model=RobotCreateOut, status_code=status.HTTP_201_CREATED)
async def create_robot(
    body: RobotCreateIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> RobotCreateOut:
    robot_id = f"robot_{secrets.token_hex(4)}"
    token = new_robot_token()
    robot = Robot(
        team_id=user.team_id,
        name=body.name,
        robot_id=robot_id,
        token_hash=_hash_token(token),
    )
    db.add(robot)
    await db.commit()
    await db.refresh(robot)
    return RobotCreateOut(robot=RobotOut.model_validate(robot), token=token)


@router.get("/{rid}", response_model=RobotOut)
async def get_robot(
    rid: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> Robot:
    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")
    return robot


@router.post("/{rid}/ui-dump", response_model=RobotUiDumpRequestOut)
async def request_ui_dump(
    rid: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> RobotUiDumpRequestOut:
    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")

    request_id = f"ui_{secrets.token_hex(8)}"
    await _send_robot_command(
        robot, {"command": "dump_ui", "request_id": request_id, "reason": "web_manual"}
    )
    return RobotUiDumpRequestOut(request_id=request_id, dispatched=True)


@router.post("/{rid}/screen/start", response_model=RobotCommandOut)
async def start_screen_stream(
    rid: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> RobotCommandOut:
    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")
    await _send_robot_command(robot, {"command": "screen_start", "interval_ms": 1000})
    return RobotCommandOut(dispatched=True)


@router.post("/{rid}/screen/stop", response_model=RobotCommandOut)
async def stop_screen_stream(
    rid: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> RobotCommandOut:
    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")
    await _send_robot_command(robot, {"command": "screen_stop"})
    return RobotCommandOut(dispatched=True)


@router.delete("/{rid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_robot(
    rid: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> None:
    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")

    conv_ids = (
        await db.execute(
            select(Conversation.id).where(Conversation.robot_id == robot.id)
        )
    ).scalars().all()
    contact_ids = (
        await db.execute(select(Contact.id).where(Contact.robot_id == robot.id))
    ).scalars().all()
    task_ids = (
        await db.execute(select(RobotTask.id).where(RobotTask.robot_id == robot.id))
    ).scalars().all()

    if conv_ids:
        await db.execute(sa_delete(AIReplyLog).where(AIReplyLog.conversation_id.in_(conv_ids)))
        await db.execute(sa_update(Message).where(Message.conversation_id.in_(conv_ids)).values(task_id=None))
    if task_ids:
        await db.execute(sa_delete(RobotTaskLog).where(RobotTaskLog.task_id.in_(task_ids)))
    await db.execute(sa_delete(RobotTaskLog).where(RobotTaskLog.robot_id == robot.id))
    await db.execute(sa_delete(RobotTask).where(RobotTask.robot_id == robot.id))
    if conv_ids:
        await db.execute(sa_delete(Message).where(Message.conversation_id.in_(conv_ids)))
        await db.execute(sa_delete(Conversation).where(Conversation.id.in_(conv_ids)))
    if contact_ids:
        await db.execute(sa_delete(UserMemory).where(UserMemory.contact_id.in_(contact_ids)))
        await db.execute(sa_delete(UserProfile).where(UserProfile.contact_id.in_(contact_ids)))
        await db.execute(sa_delete(Contact).where(Contact.id.in_(contact_ids)))
    await db.execute(sa_delete(Robot).where(Robot.id == robot.id))
    await db.commit()
    await hub.broadcast_web(user.team_id, "robot.deleted", {"robot_id": robot.robot_id, "id": rid})


@router.get("/{rid}/logs", response_model=list[RobotTaskLogOut])
async def list_robot_logs(
    rid: int,
    limit: int = 50,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> list[RobotTaskLog]:
    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")
    rows = (
        await db.execute(
            select(RobotTaskLog)
            .where(RobotTaskLog.robot_id == robot.id)
            .order_by(RobotTaskLog.created_at.desc())
            .limit(min(max(limit, 1), 200))
        )
    ).scalars().all()
    return list(rows)


@router.get("/{rid}/queue")
async def get_robot_queue(
    rid: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> dict:
    """Snapshot of the per-robot task queue — what's currently running and
    what's waiting, with priorities and wait times. Pure read."""
    from app.services import task_queue

    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")
    return task_queue.snapshot(robot.robot_id)


@router.post("/{rid}/tasks/{task_id}/cancel", response_model=RobotCommandOut)
async def cancel_robot_task(
    rid: int,
    task_id: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> RobotCommandOut:
    """Cancel a queued or currently running backend-driven device task."""
    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")
    task = await db.get(RobotTask, task_id)
    if not task or task.robot_id != robot.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "task not found")
    if task.status in {"completed", "failed", "cancelled", "timeout"}:
        raise HTTPException(status.HTTP_409_CONFLICT, f"task already {task.status}")

    from app.services import task_queue

    cancelled = await task_queue.cancel(robot.robot_id, task_id)
    if not cancelled:
        raise HTTPException(status.HTTP_409_CONFLICT, "task is not cancellable")
    return RobotCommandOut(dispatched=True)


@router.delete("/{rid}/logs", status_code=status.HTTP_204_NO_CONTENT)
async def clear_robot_logs(
    rid: int,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> None:
    """Wipe every task-log row for this robot. Tasks themselves are kept —
    only the verbose per-step log trail is dropped."""
    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")
    await db.execute(sa_delete(RobotTaskLog).where(RobotTaskLog.robot_id == robot.id))
    await db.commit()
    await hub.broadcast_web(
        user.team_id,
        "robot.logs_cleared",
        {"robot_id": robot.robot_id},
    )


@router.post("/{rid}/agent/run", response_model=AgentRunOut, status_code=status.HTTP_202_ACCEPTED)
async def run_agent_goal(
    rid: int,
    body: AgentRunIn,
    user: User = Depends(current_user),
    db: AsyncSession = Depends(get_db),
) -> AgentRunOut:
    """Fire an ad-hoc semantic instruction at the device. The ReAct agent
    plans + drives the device until `done(...)` or `max_steps` is reached.

    A synthetic RobotTask row is created so the trajectory is visible in the
    same task-log panel the rest of the system uses. The request returns
    immediately; logs stream in via the existing `task.log` WS event.
    """
    goal = (body.goal or "").strip()
    if not goal:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "goal 不能为空")
    if len(goal) > 800:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "goal 太长（最多 800 字）")
    max_steps = max(1, min(int(body.max_steps or 8), 20))

    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")
    if not settings.task_queue_enabled:
        raise HTTPException(status.HTTP_409_CONFLICT, "task queue disabled")
    if not hub.is_android_online(robot.robot_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "robot offline")

    task = RobotTask(
        robot_id=robot.id,
        type="agent_goal",
        payload_json={"goal": goal, "max_steps": max_steps, "issued_by": user.id},
        status="dispatched",
    )
    db.add(task)
    await db.flush()
    db.add(RobotTaskLog(
        robot_id=robot.id, task_id=task.id, level="info",
        message=f"agent_goal received: {goal}",
    ))
    await db.commit()
    await db.refresh(task)

    # Enqueue on the per-robot priority queue. Operator-typed goals jump
    # ahead of any pending AI auto-replies.
    from app.services import task_queue

    await task_queue.enqueue(
        robot.robot_id, "agent_goal", task.id, priority=task_queue.PRIORITY_OPERATOR
    )
    return AgentRunOut(task_id=task.id, accepted=True)


async def run_agent_goal_task(task_id: int) -> None:
    """Queue runner — invoked once the device slot opens up."""
    from app.ai.react_agent import run_react
    from app.services.send_orchestrator import append_task_log

    async with SessionLocal() as db:
        task = await db.get(RobotTask, task_id)
        if task is None:
            return
        robot = await db.get(Robot, task.robot_id)
        if robot is None:
            return
        goal = (task.payload_json or {}).get("goal") or ""
        max_steps = int((task.payload_json or {}).get("max_steps") or 8)

        async def _sink(level: str, message: str) -> None:
            async with SessionLocal() as inner:
                await append_task_log(
                    inner,
                    robot=robot,
                    task_id=task.id,
                    level=level if level in ("info", "warn", "error") else "info",
                    message=message,
                )

        ai_cfg = await settings_service.get(db, robot.team_id, "ai")
        force_llm = bool(
            ai_cfg.get("react_force_llm")
            if ai_cfg.get("react_force_llm") is not None
            else settings.react_force_llm
        )
        try:
            log.info("agent_goal start task=%s goal=%r max_steps=%d", task.id, goal, max_steps)
            result = await run_react(
                db, robot, goal,
                max_steps=max_steps,
                step_timeout=settings.react_step_timeout_sec,
                log_sink=_sink,
                force_llm=force_llm,
            )
            if asyncio.current_task() is not None and asyncio.current_task().cancelling():
                raise asyncio.CancelledError
        except asyncio.CancelledError:
            task_row = await db.get(RobotTask, task_id)
            if task_row is not None:
                task_row.status = "cancelled"
                task_row.last_error = "任务执行已中断"
                db.add(RobotTaskLog(
                    robot_id=robot.id, task_id=task.id, level="warn",
                    message="[react] cancelled by operator",
                ))
                await db.commit()
                await hub.broadcast_web(
                    robot.team_id, "task.updated",
                    {"task_id": task.id, "status": task_row.status, "error": task_row.last_error},
                )
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("agent_goal crashed task=%s", task.id)
            task_row = await db.get(RobotTask, task_id)
            if task_row is not None:
                task_row.status = "failed"
                task_row.last_error = f"agent crash: {e}"
                db.add(RobotTaskLog(
                    robot_id=robot.id, task_id=task.id, level="error",
                    message=f"agent crash: {e}",
                ))
                await db.commit()
            return

        task_row = await db.get(RobotTask, task_id)
        if task_row is None:
            return
        task_row.status = "completed" if result.ok else "failed"
        if not result.ok:
            task_row.last_error = result.summary
        db.add(RobotTaskLog(
            robot_id=robot.id, task_id=task.id,
            level="info" if result.ok else "warn",
            message=f"[react] result ok={result.ok} steps={len(result.steps)} summary={result.summary}",
        ))
        await db.commit()
        await hub.broadcast_web(
            robot.team_id, "task.updated",
            {"task_id": task.id, "status": task_row.status, "error": task_row.last_error},
        )
