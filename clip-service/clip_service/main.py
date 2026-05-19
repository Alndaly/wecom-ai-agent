from __future__ import annotations

import base64
import math
import os
from io import BytesIO
from typing import Any

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field


DEFAULT_MODEL = "jinaai/jina-clip-v2"


class EmbeddingRequest(BaseModel):
    model: str = DEFAULT_MODEL
    input: Any
    dimensions: int | None = Field(default=None, ge=16)


class ModelHolder:
    model_name: str | None = None
    model: Any = None


app = FastAPI(title="WeCom Local CLIP Service")
holder = ModelHolder()


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "ok": True,
        "model": holder.model_name,
        "loaded": holder.model is not None,
    }


@app.post("/v1/embeddings")
async def embeddings(
    body: EmbeddingRequest,
    authorization: str | None = Header(default=None),
) -> dict[str, Any]:
    check_auth(authorization)
    items = body.input if isinstance(body.input, list) else [body.input]
    if not items:
        raise HTTPException(status_code=422, detail="input required")

    model = load_model(body.model)
    parsed = [parse_input(item) for item in items]
    try:
        vectors = model.encode(parsed, convert_to_numpy=True, normalize_embeddings=True)
    except TypeError:
        vectors = model.encode(parsed, normalize_embeddings=True)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"embedding failed: {exc}") from exc

    dim = body.dimensions or env_int("CLIP_SERVICE_DIM", 0) or None
    data = []
    for idx, vector in enumerate(vectors):
        embedding = to_float_list(vector)
        if dim:
            embedding = truncate_and_normalize(embedding, dim)
        data.append({"object": "embedding", "index": idx, "embedding": embedding})
    return {
        "object": "list",
        "model": body.model,
        "data": data,
        "usage": {"prompt_tokens": 0, "total_tokens": 0},
    }


def check_auth(authorization: str | None) -> None:
    expected = os.getenv("CLIP_SERVICE_API_KEY", "").strip()
    if not expected:
        return
    if authorization != f"Bearer {expected}":
        raise HTTPException(status_code=401, detail="invalid api key")


def load_model(model_name: str) -> Any:
    local_path = os.getenv("CLIP_SERVICE_LOCAL_MODEL_PATH", "").strip()
    requested = local_path or model_name or os.getenv("CLIP_SERVICE_MODEL") or DEFAULT_MODEL
    if holder.model is not None and holder.model_name == requested:
        return holder.model
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="sentence-transformers is not installed",
        ) from exc
    try:
        revision = os.getenv("CLIP_SERVICE_REVISION", "").strip() or None
        kwargs = {"trust_remote_code": True}
        if revision and not local_path:
            kwargs["revision"] = revision
        holder.model = SentenceTransformer(requested, **kwargs)
        holder.model_name = requested
        return holder.model
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"model load failed: {exc}") from exc


def parse_input(item: Any) -> Any:
    if isinstance(item, str):
        if item.startswith("data:image/"):
            return image_from_data_url(item)
        return item
    if isinstance(item, dict):
        raw = item.get("data") or item.get("image") or item.get("url")
        if isinstance(raw, str):
            if raw.startswith("data:image/"):
                return image_from_data_url(raw)
            if raw:
                return image_from_base64(raw)
    raise HTTPException(status_code=422, detail="input must be text, data URL, or image object")


def image_from_data_url(value: str) -> Any:
    try:
        _, b64 = value.split(",", 1)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail="invalid data URL") from exc
    return image_from_base64(b64)


def image_from_base64(value: str) -> Any:
    try:
        from PIL import Image
    except ImportError as exc:
        raise HTTPException(status_code=500, detail="Pillow is not installed") from exc
    try:
        raw = base64.b64decode(value, validate=False)
        return Image.open(BytesIO(raw)).convert("RGB")
    except Exception as exc:
        raise HTTPException(status_code=422, detail="invalid image data") from exc


def to_float_list(vector: Any) -> list[float]:
    if hasattr(vector, "tolist"):
        vector = vector.tolist()
    return [float(x) for x in vector]


def truncate_and_normalize(vector: list[float], dim: int) -> list[float]:
    if dim <= 0 or dim >= len(vector):
        return vector
    out = vector[:dim]
    norm = math.sqrt(sum(x * x for x in out))
    if norm == 0:
        return out
    return [x / norm for x in out]


def env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)) or default)
    except ValueError:
        return default
