"""AppKey masking for every public API response.

The customer MES AppKey is stored in plaintext in ``tenant_registry`` (D9) and
is never allowed to leave this service through logs, traces, errors, exports,
or test snapshots. Every outbound representation of an AppKey goes through this
single function — no caller may truncate keys itself (Technology Notes).
"""

from __future__ import annotations

_MASK = "***"


def mask_app_key(app_key: str | None) -> str | None:
    """Mask an AppKey as the first 6 characters plus ``***`` (D9).

    ``None`` stays ``None``; an empty string stays empty; a key with 6 or fewer
    characters is fully masked so no usable prefix leaks. Non-ASCII keys are
    masked by code point, not by byte.
    """
    if app_key is None:
        return None
    if not app_key:
        return ""
    if len(app_key) <= 6:
        return _MASK
    return f"{app_key[:6]}{_MASK}"


__all__ = ["mask_app_key"]
