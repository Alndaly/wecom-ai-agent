import secrets
from passlib.hash import sha256_crypt

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import new_robot_token
from app.deps import current_user
from app.models import Robot, User
from app.schemas import RobotCreateIn, RobotCreateOut, RobotOut

router = APIRouter(prefix="/robots", tags=["robots"])


def _hash_token(token: str) -> str:
    # robot tokens are high-entropy; sha256 is fine here (avoid bcrypt 72-byte cap)
    return sha256_crypt.hash(token, rounds=5000)


def verify_robot_token(token: str, hashed: str) -> bool:
    return sha256_crypt.verify(token, hashed)


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
