from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.security import create_access_token, create_refresh_token, decode_token, verify_password
from app.deps import current_user
from app.models import User
from app.schemas import LoginIn, RefreshTokenIn, TokenOut, UserOut

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post("/login", response_model=TokenOut)
async def login(body: LoginIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    user = (await db.execute(select(User).where(User.email == body.email))).scalar_one_or_none()
    if not user or not verify_password(body.password, user.password_hash):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid credentials")
    return _tokens_for(user)


@router.post("/refresh", response_model=TokenOut)
async def refresh(body: RefreshTokenIn, db: AsyncSession = Depends(get_db)) -> TokenOut:
    try:
        payload = decode_token(body.refresh_token)
        if payload.get("typ") != "refresh":
            raise ValueError("not a refresh token")
        uid = int(payload["sub"])
    except Exception:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid refresh token")

    user = (await db.execute(select(User).where(User.id == uid))).scalar_one_or_none()
    if not user:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "user not found")
    return _tokens_for(user)


@router.get("/me", response_model=UserOut)
async def me(user: User = Depends(current_user)) -> UserOut:
    return UserOut.model_validate(user)


def _tokens_for(user: User) -> TokenOut:
    return TokenOut(
        access_token=create_access_token(str(user.id), {"team_id": user.team_id}),
        refresh_token=create_refresh_token(str(user.id), {"team_id": user.team_id}),
    )
