"""MinerU local CLI parser.

Invokes the `mineru` command on the backend host (must be installed via
`pip install -U "mineru[all]"`). For each input file MinerU produces a folder
containing `<name>.md` plus auxiliary assets — we just read the markdown back
and discard the rest.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shlex
import tempfile
from pathlib import Path

log = logging.getLogger(__name__)


# MinerU 3.x supports these extensions natively.
SUPPORTED_EXTS = {".pdf", ".png", ".jpg", ".jpeg", ".bmp", ".tiff", ".webp",
                  ".docx", ".pptx", ".xlsx"}


async def parse(
    name: str,
    data: bytes,
    *,
    cmd: str = "mineru",
    extra_args: str = "",
    timeout_sec: int = 600,
) -> str:
    """Run the local `mineru` CLI and return markdown text."""
    safe_name = _safe_name(name)
    with tempfile.TemporaryDirectory(prefix="mineru_") as tmp:
        tmp_path = Path(tmp)
        input_path = tmp_path / safe_name
        output_dir = tmp_path / "out"
        output_dir.mkdir()
        input_path.write_bytes(data)

        argv = [cmd, "-p", str(input_path), "-o", str(output_dir)]
        if extra_args.strip():
            argv.extend(shlex.split(extra_args))

        log.info("mineru_local: %s", " ".join(shlex.quote(a) for a in argv))
        proc = await asyncio.create_subprocess_exec(
            *argv,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(), timeout=timeout_sec
            )
        except asyncio.TimeoutError:
            proc.kill()
            raise RuntimeError(f"mineru cli timed out after {timeout_sec}s")

        if proc.returncode != 0:
            err = (stderr or b"").decode("utf-8", errors="replace")[:2000]
            raise RuntimeError(f"mineru cli exit={proc.returncode}: {err}")

        return _collect_markdown(output_dir)


def _safe_name(name: str) -> str:
    base = os.path.basename(name) or "input"
    # avoid spaces / odd chars confusing the CLI
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in base)


def _collect_markdown(out_dir: Path) -> str:
    """MinerU writes one .md per input file, possibly nested under a folder
    named after the input. Concatenate all .md found (in stable order)."""
    mds = sorted(out_dir.rglob("*.md"))
    if not mds:
        raise RuntimeError("mineru produced no markdown output")
    parts: list[str] = []
    for md in mds:
        try:
            parts.append(md.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            parts.append(md.read_text(encoding="utf-8", errors="replace"))
    text = "\n\n".join(p.strip() for p in parts if p.strip())
    if not text:
        raise RuntimeError("mineru output is empty")
    return text
