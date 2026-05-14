from __future__ import annotations

import secrets
from datetime import datetime, timedelta, timezone

import bcrypt
import jwt

from .config import settings


# bcrypt has a 72-byte input cap. Realistic passwords are well under,
# but we truncate defensively so callers can't trigger the underlying
# library's ValueError. (This matches passlib's documented behaviour
# in 1.7.5+.)
_BCRYPT_MAX = 72


def _clip(b: bytes) -> bytes:
    return b[:_BCRYPT_MAX]


def hash_password(p: str) -> str:
    hashed = bcrypt.hashpw(_clip(p.encode("utf-8")), bcrypt.gensalt())
    return hashed.decode("utf-8")


def verify_password(p: str, hashed: str) -> bool:
    try:
        return bcrypt.checkpw(_clip(p.encode("utf-8")), hashed.encode("utf-8"))
    except (ValueError, TypeError):
        return False


def create_access_token(sub: str, extra: dict | None = None) -> str:
    now = datetime.now(timezone.utc)
    payload = {
        "sub": sub,
        "iat": int(now.timestamp()),
        "exp": int((now + timedelta(minutes=settings.jwt_expire_min)).timestamp()),
        **(extra or {}),
    }
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def decode_token(token: str) -> dict:
    return jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])


def new_robot_token() -> str:
    return secrets.token_urlsafe(32)
