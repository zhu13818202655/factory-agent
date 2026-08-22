from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class InteractionSandboxPolicy:
    database: str = ":memory:"
    allow_external_access: bool = False
    allow_unsigned_extensions: bool = False
    allow_ddl: bool = False
    allow_dml: bool = False
