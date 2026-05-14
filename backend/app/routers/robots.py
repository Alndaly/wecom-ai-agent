import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.security import new_robot_token
from app.core.ws_manager import hub
from app.deps import current_user
from app.models import Robot, RobotTaskLog, User
from app.schemas import (
    RobotCommandOut,
    RobotCreateIn,
    RobotCreateOut,
    RobotOut,
    RobotTaskLogOut,
    RobotUiDumpRequestOut,
)

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
    await db.delete(robot)
    await db.commit()


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
