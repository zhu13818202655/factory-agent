from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, Mapping, TypeVar, override

import httpx
from pydantic import BaseModel

from factory_agent.ports import MesDataSource

ResponseT = TypeVar("ResponseT", bound=BaseModel)


@dataclass(frozen=True, slots=True)
class CanonicalRequest(Generic[ResponseT]):
    operation_id: str
    query: tuple[tuple[str, str], ...]
    response_model: type[ResponseT]


class CanonicalMesAdapter(
    MesDataSource[CanonicalRequest[ResponseT], ResponseT], Generic[ResponseT]
):
    def __init__(self, base_url: str, operation_paths: Mapping[str, str]) -> None:
        self._base_url = base_url
        self._operation_paths = dict(operation_paths)

    @override
    async def execute(self, request: CanonicalRequest[ResponseT]) -> ResponseT:
        try:
            path = self._operation_paths[request.operation_id]
        except KeyError as error:
            raise ValueError(f"unknown Canonical operation: {request.operation_id}") from error

        async with httpx.AsyncClient(base_url=self._base_url) as client:
            response = await client.get(path, params=request.query)
            response.raise_for_status()
            return request.response_model.model_validate(response.json())
