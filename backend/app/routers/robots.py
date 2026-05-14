import hashlib
import hmac
import secrets

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.db import get_db
from app.core.security import new_robot_token
from app.deps import current_user
from app.models import Robot, User
from app.schemas import RobotCreateIn, RobotCreateOut, RobotOut

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


@router.delete("/{rid}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_robot(
    rid: int, user: User = Depends(current_user), db: AsyncSession = Depends(get_db)
) -> None:
    robot = await db.get(Robot, rid)
    if not robot or robot.team_id != user.team_id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "robot not found")
    await db.delete(robot)
    await db.commit()
