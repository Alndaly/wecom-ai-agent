"""Document parsing → plain text / markdown.

Two layers:
  - `parse(name, mime, data)` — legacy sync entrypoint, supports
    .txt / .md / .pdf via pypdf. Kept for tests and back-compat.
  - `parse_for_team(db, team_id, name, mime, data)` — async, reads the
    team's runtime config and dispatches to the configured backend
    (builtin / mineru_local / mineru_api).
"""
from __future__ import annotations

import logging
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings as env_settings
from app.services import settings_service

from . import mineru_api, mineru_local

log = logging.getLogger(__name__)


def parse(name: str, mime: str, data: bytes) -> str:
    """Builtin parser: text + pypdf only."""
    name_lower = name.lower()
    if name_lower.endswith((".md", ".txt")) or mime.startswith("text/"):
        return _decode(data)
    if name_lower.endswith(".pdf") or mime == "application/pdf":
        return _parse_pdf(data)
    try:
        return _decode(data)
    except Exception as e:
        raise RuntimeError(f"unsupported document: {name} ({mime}): {e}") from e


async def parse_for_team(
    db: AsyncSession,
    team_id: int,
    name: str,
    mime: str,
    data: bytes,
) -> str:
    cfg = await settings_service.get(db, team_id, "parser")
    return await parse_with_cfg(name, mime, data, cfg=cfg)


async def parse_with_cfg(
    name: str,
    mime: str,
    data: bytes,
    *,
    cfg: dict[str, Any],
) -> str:
    backend = (cfg.get("backend") or "builtin").strip()
    name_lower = name.lower()
    is_text = name_lower.endswith((".md", ".txt")) or mime.startswith("text/")

    # Plain-text inputs never need a heavy parser — short-circuit.
    if is_text:
        return _decode(data)

    if backend == "mineru_local":
        return await mineru_local.parse(
            name,
            data,
            cmd=cfg.get("local_cmd") or env_settings.mineru_local_cmd,
            extra_args=cfg.get("local_extra_args") or "",
            timeout_sec=env_settings.mineru_timeout_sec,
        )
    if backend == "mineru_api":
        return await mineru_api.parse(
            name,
            data,
            api_base=cfg.get("api_base") or env_settings.mineru_api_base,
            token=cfg.get("api_key") or env_settings.mineru_api_token,
            model_version=cfg.get("model_version")
            or env_settings.mineru_model_version,
            timeout_sec=env_settings.mineru_timeout_sec,
        )

    return parse(name, mime, data)


def _decode(data: bytes) -> str:
    for enc in ("utf-8", "utf-8-sig", "gbk", "gb18030", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _parse_pdf(data: bytes) -> str:
    try:
        from pypdf import PdfReader  # type: ignore
    except ImportError as e:
        raise RuntimeError("pypdf not installed; pip install pypdf") from e
    import io

    reader = PdfReader(io.BytesIO(data))
    parts = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n\n".join(parts).strip()
