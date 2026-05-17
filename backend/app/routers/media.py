from __future__ import annotations

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import FileResponse
from sqlalchemy import select

from app.core.db import SessionLocal
from app.models import Message
from app.services.media_store import resolve_media_path

router = APIRouter(prefix="/media", tags=["media"])


@router.get("/{token}")
async def get_media(token: str) -> FileResponse:
    async with SessionLocal() as db:
        msg = (
            await db.execute(
                select(Message).where(
                    Message.media_json["download_token"].as_string() == token
                )
            )
        ).scalar_one_or_none()
        if msg is None or not msg.media_json:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "media not found")
        path = resolve_media_path(msg.media_json)
        if path is None or not path.exists():
            raise HTTPException(status.HTTP_404_NOT_FOUND, "media file not found")
        return FileResponse(
            path,
            media_type=str(msg.media_json.get("mime") or "application/octet-stream"),
            filename=str(msg.media_json.get("filename") or path.name),
        )
