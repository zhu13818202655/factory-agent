"""Filename and spreadsheet-cell safety for exported artifacts.

Two distinct threats are addressed:

1. ``sanitize_filename`` returns a safe basename: it strips path separators and
   control characters so a role/function/time label can never traverse the
   filesystem, and it drops leading formula/command markers so the produced
   filename cannot be interpreted as a shell or spreadsheet formula.
2. ``safe_cell_text`` neutralises user/text-derived cell values that begin with
   a spreadsheet formula marker (``=`` ``+`` ``-`` ``@``) by prefixing an
   apostrophe, so an exported cell is always rendered as text, never executed.
"""

from __future__ import annotations

import re
from typing import Any

_UNSAFE_CHARS = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff_\-.]")
_FORMULA_MARKERS = "=+-@"
_CELL_MARKERS = "=+-@\t\r"


def sanitize_filename(raw: str, *, max_length: int = 120, fallback: str = "export") -> str:
    """Return a safe, traversal-proof basename (no extension handling)."""
    cleaned = _UNSAFE_CHARS.sub("_", raw)
    cleaned = cleaned.strip().strip("._")
    cleaned = cleaned.lstrip(_FORMULA_MARKERS)
    cleaned = cleaned.replace("/", "_").replace("\\", "_")
    if not cleaned or cleaned in (".", ".."):
        cleaned = fallback
    return cleaned[:max_length]


def safe_cell_text(value: Any) -> Any:
    """Return a value safe to write as text; formula markers are neutralised."""
    if not isinstance(value, str):
        return value
    if value.startswith(_CELL_MARKERS):
        return f"'{value}"
    return value


def build_export_filename(
    role: str,
    function: str,
    time_range_label: str,
    generated_at: str,
    *,
    extension: str = ".xlsx",
) -> str:
    """Compose ``角色_功能_时间范围_生成时间.xlsx`` with each part sanitized."""
    parts = (role, function, time_range_label, generated_at)
    safe = "_".join(sanitize_filename(part) for part in parts)
    return f"{safe}{extension}"


__all__ = [
    "build_export_filename",
    "safe_cell_text",
    "sanitize_filename",
]
