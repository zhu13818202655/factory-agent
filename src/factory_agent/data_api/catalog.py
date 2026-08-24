"""API Catalog: reviewed registry of approved MES operations.

Catalog entries are loaded from ``configs/knowledge/apis.yaml`` and validated
against a strict schema. Unreviewed or malformed entries can never reach the
runtime registry. Authorization-critical parameters must be declared with the
``scope`` source, meaning they may only originate from ``DataScope``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from factory_agent.domain.errors import InvalidRequestError, UnsupportedOperationError

DEFAULT_CATALOG_PATH = Path("configs/knowledge/apis.yaml")

ParameterSource = Literal["scope", "filter", "clock"]
PaginationKind = Literal["none", "items_total_page_size"]


class CatalogOperation(BaseModel):
    """One reviewed operation entry; immutable once loaded."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation_id: str
    path: str
    kind: Literal["identity", "resource"]
    resource: str | None = None
    parameter_sources: dict[str, ParameterSource]
    pagination: PaginationKind
    timeout_seconds: float
    min_role: Literal["employee", "manager", "owner"]
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
        if operation.pagination == "items_total_page_size" and operation.kind == "identity":
            # A3 (identity listing) legitimately uses the page envelope.
            if operation.operation_id != "A3_listEffectiveScopes":
                raise InvalidRequestError("only resource operations use page envelopes")
        operations[operation.operation_id] = operation

    return ApiCatalog(version=document.version, _operations=operations)


__all__ = [
    "ApiCatalog",
    "ApiCatalogDocument",
    "CatalogOperation",
    "DEFAULT_CATALOG_PATH",
    "load_catalog",
]
