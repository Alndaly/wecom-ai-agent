from __future__ import annotations

from fastapi import APIRouter, Header, HTTPException, status

from app.core.config import settings
from app.services import task_queue

router = APIRouter(prefix="/internal/tasks", tags=["internal-tasks"])


@router.post("/drain/{robot_id}")
async def drain_robot_queue(
    robot_id: str,
    x_task_executor_secret: str | None = Header(default=None),
) -> dict:
    if settings.task_executor_secret and x_task_executor_secret != settings.task_executor_secret:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid task executor secret")
    ok = await task_queue.drain_robot(robot_id)
    if not ok:
        raise HTTPException(status.HTTP_423_LOCKED, "robot task queue is already draining")
    return {"ok": True, "robot_id": robot_id}
