"""Message retention sweeper.

Periodically deletes messages older than `settings.message_retention_days`.
Runs forever as a background task started from the FastAPI lifespan; cancels
cleanly on shutdown.

Design notes:
- Sweep deletes *messages*, not conversations — empty conversations are kept
  so the contact + profile remain visible on the workbench.
- Detaches FKs that reference messages before deleting (AIReplyLog.message_id,
  Message.task_id) so the DELETE succeeds without violating constraints.
- Failures are logged and the loop continues; one bad sweep should not kill
  the background task.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import timedelta

from sqlalchemy import delete as sa_delete, select, update as sa_update

from app.core.config import settings
from app.core.db import SessionLocal
from app.models import AIReplyLog, Message, RobotTask, utcnow

log = logging.getLogger(__name__)


async def sweep_once() -> int:
    """Run one retention pass. Returns the number of messages deleted."""
    days = int(settings.message_retention_days or 0)
    if days <= 0:
        return 0
    cutoff = utcnow() - timedelta(days=days)
    async with SessionLocal() as db:
        ids = (
            await db.execute(select(Message.id).where(Message.created_at < cutoff))
        ).scalars().all()
        if not ids:
            return 0
        # Detach references first
        await db.execute(
            sa_update(AIReplyLog).where(AIReplyLog.message_id.in_(ids)).values(message_id=None)
        )
        # Avoid Message.task_id FK violation if related tasks linger — clear
        # the column on the rows we're about to drop.
        await db.execute(
            sa_update(Message).where(Message.id.in_(ids)).values(task_id=None)
        )
        # RobotTask.message_id is a soft int column (no FK), nothing to detach.
        await db.execute(sa_delete(Message).where(Message.id.in_(ids)))
        await db.commit()
        log.info("retention: deleted %d message(s) older than %s", len(ids), cutoff.isoformat())
        return len(ids)


async def run_loop() -> None:
    """Long-running sweeper. Cancel-safe."""
    if int(settings.message_retention_days or 0) <= 0:
        log.info("retention disabled (message_retention_days <= 0)")
        return
    # First sweep after a short delay so we don't compete with app startup.
    try:
        await asyncio.sleep(60)
        while True:
            try:
                await sweep_once()
            except Exception:  # noqa: BLE001
                log.exception("retention sweep failed")
            await asyncio.sleep(max(60, int(settings.retention_sweep_interval_sec)))
    except asyncio.CancelledError:
        log.info("retention loop cancelled")
        raise
