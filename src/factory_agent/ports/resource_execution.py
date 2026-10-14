from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

from factory_agent.domain import Role
from factory_agent.ports.contracts import ResourceFetchResult


@dataclass(frozen=True, slots=True)
class ExecutionRequest:
    operation_id: str
    time_range: tuple[datetime, datetime]
    pagination_size: int = 50
    role: Role | None = None
    result_validator: Callable[[ResourceFetchResult], bool] | None = field(default=None, repr=False)
    allow_owner_snapshot: bool = True
