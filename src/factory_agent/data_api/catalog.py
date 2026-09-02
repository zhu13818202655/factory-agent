"""API Catalog: reviewed registry of approved MES operations.

Catalog entries are loaded from ``configs/knowledge/apis.yaml`` and validated
against a strict schema. Unreviewed or malformed entries can never reach the
runtime registry.

Registry semantics:
- ``parameter_sources`` gains the ``credential`` category: those parameters may
  only originate from ``MesCredentialBundle`` (app_key/timestamp/sign), never
  from filters or model output.
- ``enabled: false`` operations (MoveMenuQuery, K7) load but are rejected at
  runtime before any HTTP traffic.
- ``required_params`` are validated at load time against the customer contract
  (e.g. GongziMxQuery.Type/scheme, WorktypeProgressQuery.userid).
- Pagination is ``list_total``: walk pages until accumulated rows reach
  ``result.total`` (M13).
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from factory_agent.domain.errors import InvalidRequestError, UnsupportedOperationError

DEFAULT_CATALOG_PATH = Path("configs/knowledge/apis.yaml")

ParameterSource = Literal["credential", "scope", "filter", "clock"]
PaginationKind = Literal["none", "list_total"]
#: MES 调用统计的计费分类（D1/D5）。能力分类 ≠ API 分类（R2），该字段只在
#: ``data_api`` 内部消费，绝不作为能力维度统计。
UsageCategory = Literal["output", "payroll", "order", "other"]

_USAGE_CATEGORIES: frozenset[str] = frozenset({"output", "payroll", "order", "other"})


class CatalogOperation(BaseModel):
    """One reviewed operation entry; immutable once loaded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    path: str
    kind: Literal["identity", "resource"]
    resource: str | None = None
    enabled: bool = True
    required_params: tuple[str, ...] = ()
    parameter_sources: dict[str, ParameterSource]
    pagination: PaginationKind
    usage_category: UsageCategory | None = None
    supports_footer: bool = False
    timeout_seconds: float
    sensitive_fields: tuple[str, ...] = ()
    related_keys: tuple[str, ...] = ()


class ApiCatalogDocument(BaseModel):
    """Top-level catalog document schema."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    operations: tuple[CatalogOperation, ...]


@dataclass(frozen=True, slots=True)
class ApiCatalog:
    """Immutable runtime view of the reviewed catalog."""

    version: int
    _operations: dict[str, CatalogOperation]

    def get(self, operation_id: str) -> CatalogOperation:
        try:
            return self._operations[operation_id]
        except KeyError as error:
            raise UnsupportedOperationError(
                "operation is not registered in the reviewed catalog"
            ) from error

    def __contains__(self, operation_id: object) -> bool:
        return isinstance(operation_id, str) and operation_id in self._operations

    @property
    def operation_ids(self) -> frozenset[str]:
        return frozenset(self._operations)


def load_catalog(path: Path | None = None) -> ApiCatalog:
    """Load and validate the catalog; failures block startup."""
    catalog_path = path or DEFAULT_CATALOG_PATH
    try:
        raw = yaml.safe_load(catalog_path.read_text(encoding="utf-8"))
    except FileNotFoundError as error:
        raise InvalidRequestError(f"catalog file is missing: {catalog_path}") from error
    except yaml.YAMLError as error:
        raise InvalidRequestError("catalog file is not valid YAML") from error

    try:
        document = ApiCatalogDocument.model_validate(raw)
    except ValidationError as error:
        raise InvalidRequestError("catalog failed schema validation") from error

    operations: dict[str, CatalogOperation] = {}
    for operation in document.operations:
        if operation.operation_id in operations:
            raise InvalidRequestError("catalog contains duplicate operation IDs")
        if operation.kind == "resource" and operation.resource is None:
            raise InvalidRequestError("resource operations must declare a resource name")
        if operation.pagination == "list_total" and operation.kind == "identity":
            raise InvalidRequestError("only resource operations use list envelopes")
        # Credential-sourced parameters can never come from filters or models.
        for parameter, source in operation.parameter_sources.items():
            if source == "credential" and parameter not in {
                "app_key",
                "timestamp",
                "sign",
            }:
                raise InvalidRequestError(
                    f"{operation.operation_id}.{parameter} has an invalid credential source"
                )
        # Every reviewed operation must be archived to one of the four billing
        # categories (D1/D5); a new operation without an archive blocks startup
        # so the MES usage statistics never silently drop a call category.
        if operation.usage_category not in _USAGE_CATEGORIES:
            raise InvalidRequestError(
                f"{operation.operation_id} is missing a reviewed usage_category "
                f"(expected one of {sorted(_USAGE_CATEGORIES)})"
            )
        operations[operation.operation_id] = operation

    return ApiCatalog(version=document.version, _operations=operations)


__all__ = [
    "ApiCatalog",
    "ApiCatalogDocument",
    "CatalogOperation",
    "DEFAULT_CATALOG_PATH",
    "load_catalog",
]
