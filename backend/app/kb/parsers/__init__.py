"""Document parsing → plain text.

Keep dependency footprint small. PDF support is opt-in (pypdf) and falls back
to a clear error message if the package isn't installed.
"""
from __future__ import annotations


def parse(name: str, mime: str, data: bytes) -> str:
    name_lower = name.lower()
    if name_lower.endswith((".md", ".txt")) or mime.startswith("text/"):
        return _decode(data)
    if name_lower.endswith(".pdf") or mime == "application/pdf":
        return _parse_pdf(data)
    # Fallback: try decode as text
    try:
        return _decode(data)
    except Exception as e:
        raise RuntimeError(f"unsupported document: {name} ({mime}): {e}") from e


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
