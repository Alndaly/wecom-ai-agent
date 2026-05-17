from __future__ import annotations

import logging

import httpx
from celery import Celery

from app.core.config import settings

log = logging.getLogger(__name__)

celery_app = Celery(
    "wecom_ai_agent",
    broker=settings.celery_broker_url,
    backend=settings.celery_result_backend,
)

celery_app.conf.update(
    task_acks_late=True,
    worker_prefetch_multiplier=1,
    task_default_queue="device_tasks",
    task_routes={"app.worker.drain_robot_queue": {"queue": "device_tasks"}},
)


@celery_app.task(
    name="app.worker.drain_robot_queue",
    bind=True,
    autoretry_for=(httpx.TimeoutException, httpx.TransportError),
    retry_backoff=True,
    retry_jitter=True,
    max_retries=None,
)
def drain_robot_queue(self, robot_id: str) -> dict:
    """Wake the backend process that owns Android WS connections."""
    url = f"{settings.task_executor_base_url.rstrip('/')}/internal/tasks/drain/{robot_id}"
    headers = {}
    if settings.task_executor_secret:
        headers["X-Task-Executor-Secret"] = settings.task_executor_secret
    with httpx.Client(timeout=settings.task_executor_timeout_sec) as client:
        resp = client.post(url, headers=headers)
    if resp.status_code == 423:
        raise self.retry(countdown=1)
    if resp.status_code >= 500:
        raise self.retry(countdown=2)
    resp.raise_for_status()
    return resp.json()
