from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class SandboxTable:
    name: str
    rows: tuple[dict[str, Any], ...]
    columns: tuple[tuple[str, str], ...]


class InteractionSandboxPort(Protocol):
    def register_table(self, table: SandboxTable) -> None: ...

    def execute_typed(
        self, sql: str, params: Mapping[str, Any] | Sequence[Any] | None = None
    ) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]: ...

    def interrupt(self) -> None: ...

    def close(self) -> None: ...
