from __future__ import annotations

import asyncio
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
    await _bootstrap_agent_tools()
    await _hydrate_vector_store()
    await _bootstrap_task_queue()
    await _bootstrap_auto_reply_scheduler()
    # background retention sweeper — cancel on shutdown
    from app.services import auto_reply_scheduler as _ars, retention, task_queue as _tq
    retention_task = asyncio.create_task(retention.run_loop(), name="retention-loop")
    try:
        yield
    finally:
        retention_task.cancel()
        try:
            await retention_task
        except (asyncio.CancelledError, Exception):
            pass
        try:
            await _ars.shutdown()
        except Exception:  # noqa: BLE001
            logging.exception("auto reply scheduler shutdown failed")
        try:
            await _tq.shutdown()
        except Exception:  # noqa: BLE001
            logging.exception("task queue shutdown failed")
        # Close MCP sessions on shutdown so subprocesses don't linger.
        try:
            from app.ai.tools import mcp_adapter

            await mcp_adapter.shutdown()
        except Exception:  # noqa: BLE001
            logging.exception("mcp shutdown failed")


async def _hydrate_vector_store() -> None:
    """Mirror SQL-stored embeddings into the active vector store.

    The SQL `knowledge_chunks.embedding_json` is our source-of-truth for
    embeddings (we write it at ingest time). This step lets two scenarios
    "just work":
      1. memory store, restart → repopulate the empty in-memory store.
      2. switched from memory → milvus → fresh milvus collection has no data
         but SQL still does. Upsert is idempotent so it's safe to run every
         start; for already-populated milvus deployments it's a no-op write.

    Failures here MUST NOT block startup.
    """
    try:
        from app.kb.vectorstore import get_vector_store
        from app.models import KnowledgeBase, KnowledgeChunk

        store = get_vector_store()
        async with SessionLocal() as db:
            rows = (
                await db.execute(
                    select(KnowledgeChunk, KnowledgeBase.team_id)
                    .join(KnowledgeBase, KnowledgeBase.id == KnowledgeChunk.kb_id)
                    .where(KnowledgeChunk.embedding_json.is_not(None))
                )
            ).all()
        if not rows:
            logging.info("vector hydrate: no chunks to load")
            return
        ids: list[str] = []
        vecs: list[list[float]] = []
        metas: list[dict] = []
        for chunk, team_id in rows:
            ids.append(chunk.embedding_id or f"chunk-{chunk.id}")
            vecs.append(chunk.embedding_json)
            metas.append({
                "team_id": team_id,
                "kb_id": chunk.kb_id,
                "doc_id": chunk.doc_id,
                "chunk_id": chunk.id,
                "text": chunk.text,
            })
        await store.upsert(ids, vecs, metas)
        logging.info(
            "vector hydrate: restored %d chunk(s) into %s store",
            len(ids), settings.vector_store,
        )
    except Exception:
        logging.exception("vector hydrate failed (continuing without it)")


async def _bootstrap_task_queue() -> None:
    """Register every runner with the per-robot priority queue, then re-queue
    any tasks that were `dispatched` when the process last died."""
    if not settings.task_queue_enabled:
        logging.info("task queue disabled; skip runner registration and recovery")
        return
    try:
        from app.services import task_queue
        from app.services.send_orchestrator import run_send_task
        from app.routers.robots import run_agent_goal_task

        task_queue.register_runner("send_text", run_send_task)
        task_queue.register_runner("agent_goal", run_agent_goal_task)
        n = await task_queue.recover_pending_tasks()
        if n:
            logging.info("task queue: recovered %d in-flight task(s) from previous run", n)
    except Exception:  # noqa: BLE001
        logging.exception("task queue bootstrap failed")


async def _bootstrap_auto_reply_scheduler() -> None:
    try:
        from app.services import auto_reply_scheduler

        await auto_reply_scheduler.recover_pending()
    except Exception:  # noqa: BLE001
        logging.exception("auto reply scheduler bootstrap failed")


async def _bootstrap_agent_tools() -> None:
    """Register built-in tools, load file skills, connect MCP servers.

    Failures here MUST NOT block app startup — log and move on.
    """
    try:
        from app.ai.tools import builtin, mcp_adapter, skills as skills_loader

        builtin.register_builtins()
        try:
            skills_loader.load_skills_from_dir(settings.skills_dir)
        except Exception:  # noqa: BLE001
            logging.exception("skill loader crashed")
        servers = mcp_adapter.parse_servers(settings.mcp_servers_json)
        if servers:
            try:
                await mcp_adapter.connect_servers(servers)
            except Exception:  # noqa: BLE001
                logging.exception("mcp connect failed")
    except Exception:  # noqa: BLE001
        logging.exception("agent bootstrap failed")


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
