from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy import select

from app.core.config import settings
from app.core.db import SessionLocal, init_db
from app.core.security import hash_password
from app.models import Team, User
from app.routers import ai as ai_router
from app.routers import auth, conversations, kb, memory, robots
from app.routers import settings as settings_router
from app.routers import ui_analysis
from app.ws import android as ws_android
from app.ws import web as ws_web

logging.basicConfig(level=settings.log_level)


async def _ensure_seed() -> None:
    async with SessionLocal() as db:
        u = (await db.execute(select(User).limit(1))).scalar_one_or_none()
        if u:
            return
        team = Team(name="Default")
        db.add(team)
        await db.flush()
        admin = User(
            team_id=team.id,
            email="admin@example.com",
            password_hash=hash_password("admin123"),
            display_name="Admin",
        )
        db.add(admin)
        await db.commit()
        logging.info("seeded admin@example.com / admin123 (team_id=%s)", team.id)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await init_db()
    await _ensure_seed()
    yield


app = FastAPI(title="WeCom AI Agent", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origin_list,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth.router)
app.include_router(robots.router)
app.include_router(conversations.router)
app.include_router(ai_router.router)
app.include_router(kb.router)
app.include_router(memory.router)
app.include_router(settings_router.router)
app.include_router(ui_analysis.router)
app.include_router(ws_web.router)
app.include_router(ws_android.router)


@app.get("/healthz")
async def healthz() -> dict[str, str]:
    return {"status": "ok"}
