from __future__ import annotations

import asyncio
import logging
import os
import subprocess
import sys
from collections.abc import AsyncIterator
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from .config import settings

log = logging.getLogger(__name__)


class Base(DeclarativeBase):
    pass


engine = create_async_engine(settings.database_url, echo=False, future=True)
SessionLocal = async_sessionmaker(engine, expire_on_commit=False, class_=AsyncSession)


async def get_db() -> AsyncIterator[AsyncSession]:
    async with SessionLocal() as session:
        yield session


async def init_db() -> None:
    """Run pending Alembic migrations to bring the schema to `head`.

    We invoke the alembic CLI as a subprocess. Two reasons over the previous
    `alembic.command.upgrade` + `asyncio.to_thread` approach:

      1. alembic ≥ 1.18 + sqlalchemy 2.0.49 hang when invoked from a worker
         thread inside an asyncio loop; the CLI in a child process is bulletproof.
      2. Subprocess gives us a clean DDL log on stdout that we can stream to
         our own logger — useful when a migration goes sideways in prod.
    """
    from app import models  # noqa: F401  ensure metadata is loaded

    await asyncio.to_thread(_run_alembic_upgrade)


def _run_alembic_upgrade() -> None:
    backend_root = Path(__file__).resolve().parents[2]
    log.info("alembic: upgrading to head…")
    env = {**os.environ, "DATABASE_URL": settings.database_url}
    # PythonPath: same interpreter that's running the app, so the env's
    # alembic shim picks up the venv's sqlalchemy / drivers.
    python = sys.executable
    proc = subprocess.run(
        [python, "-m", "alembic", "upgrade", "head"],
        cwd=str(backend_root),
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    if proc.stdout:
        for line in proc.stdout.splitlines():
            log.info("alembic | %s", line)
    if proc.returncode != 0:
        log.error("alembic: upgrade failed\n%s", proc.stderr)
        raise RuntimeError(f"alembic upgrade failed: {proc.stderr.strip()[:500]}")
    log.info("alembic: schema up to date")
