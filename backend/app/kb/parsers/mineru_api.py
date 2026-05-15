"""MinerU cloud REST parser (mineru.net).

Flow (v4):
  1. POST  /file-urls/batch       → { batch_id, file_urls: [presigned PUT] }
  2. PUT   {presigned}            → upload the raw bytes
  3. GET   /extract-results/batch/{batch_id}  (poll until state == "done")
  4. GET   {full_zip_url}         → download a zip; extract the first .md
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
import zipfile

import httpx

log = logging.getLogger(__name__)

_POLL_INTERVAL_SEC = 3.0
_TERMINAL_OK = {"done"}
_TERMINAL_FAIL = {"failed"}


async def parse(
    name: str,
    data: bytes,
    *,
    api_base: str,
    token: str,
    model_version: str = "vlm",
    timeout_sec: int = 600,
) -> str:
    if not token.strip():
        raise RuntimeError("mineru api token not configured")

    api_base = api_base.rstrip("/")
    headers = {"Authorization": f"Bearer {token}"}
    safe_name = os.path.basename(name) or "input.pdf"

    async with httpx.AsyncClient(timeout=60.0) as client:
        # Step 1: request a presigned upload URL.
        r = await client.post(
            f"{api_base}/file-urls/batch",
            headers={**headers, "Content-Type": "application/json"},
            json={
                "files": [{"name": safe_name}],
                "model_version": model_version,
            },
        )
        r.raise_for_status()
        envelope = r.json()
        if envelope.get("code") not in (0, 200):
            raise RuntimeError(f"mineru api: {envelope.get('msg') or envelope}")
        payload = envelope.get("data") or {}
        batch_id = payload.get("batch_id")
        file_urls = payload.get("file_urls") or []
        if not batch_id or not file_urls:
            raise RuntimeError(f"mineru api: malformed response {envelope}")

        # Step 2: PUT the file to the presigned URL. No auth headers — the URL
        # itself is signed. Use a longer timeout because uploads can be slow.
        async with httpx.AsyncClient(timeout=timeout_sec) as upload_client:
            up = await upload_client.put(file_urls[0], content=data)
        up.raise_for_status()

        # Step 3: poll the result endpoint.
        full_zip_url, err_msg = await _poll(
            client,
            f"{api_base}/extract-results/batch/{batch_id}",
            headers,
            safe_name,
            timeout_sec,
        )
        if err_msg:
            raise RuntimeError(f"mineru extract failed: {err_msg}")
        if not full_zip_url:
            raise RuntimeError("mineru extract: no zip url")

        # Step 4: download the zip and pull the first .md out.
        zr = await client.get(full_zip_url, timeout=timeout_sec)
        zr.raise_for_status()
        return _markdown_from_zip(zr.content)


async def _poll(
    client: httpx.AsyncClient,
    url: str,
    headers: dict,
    file_name: str,
    timeout_sec: int,
) -> tuple[str | None, str | None]:
    deadline = asyncio.get_event_loop().time() + timeout_sec
    while True:
        r = await client.get(url, headers=headers)
        r.raise_for_status()
        env = r.json()
        if env.get("code") not in (0, 200):
            raise RuntimeError(f"mineru poll: {env.get('msg') or env}")
        data = env.get("data") or {}
        for row in data.get("extract_result") or []:
            if row.get("file_name") != file_name:
                continue
            state = (row.get("state") or "").lower()
            if state in _TERMINAL_OK:
                return row.get("full_zip_url"), None
            if state in _TERMINAL_FAIL:
                return None, row.get("err_msg") or "failed"
            log.debug("mineru state=%s", state)
            break
        if asyncio.get_event_loop().time() > deadline:
            raise RuntimeError(f"mineru poll timed out after {timeout_sec}s")
        await asyncio.sleep(_POLL_INTERVAL_SEC)


def _markdown_from_zip(blob: bytes) -> str:
    with zipfile.ZipFile(io.BytesIO(blob)) as zf:
        md_names = sorted(n for n in zf.namelist() if n.lower().endswith(".md"))
        if not md_names:
            raise RuntimeError("mineru zip contains no markdown")
        parts: list[str] = []
        for n in md_names:
            with zf.open(n) as fh:
                parts.append(fh.read().decode("utf-8", errors="replace"))
        text = "\n\n".join(p.strip() for p in parts if p.strip())
        if not text:
            raise RuntimeError("mineru markdown is empty")
        return text
