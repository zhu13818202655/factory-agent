from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from factory_agent.data_api.catalog import DEFAULT_CATALOG_PATH, load_catalog


def test_default_catalog_loads_and_covers_27_customer_operations() -> None:
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    assert len(catalog.operation_ids) == 27
    assert "SystemToken" in catalog
    assert "YskQuery" in catalog
    assert "GongziMxQuery" in catalog
    assert "MoveMenuQuery" in catalog  # registered but disabled (K7)
    assert catalog.get("MoveMenuQuery").enabled is False
    assert "A1_getTenantMembership" not in catalog
    assert "C1_listPieceworkRecords" not in catalog


def test_unknown_operation_is_rejected_at_runtime() -> None:
    from factory_agent.domain.errors import UnsupportedOperationError

    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    with pytest.raises(UnsupportedOperationError):
        catalog.get("X9_notRegistered")


def test_scope_sourced_params_only_carry_data_scope_identifiers() -> None:
    """``scope`` params may only originate from DataScope (uid/Uid), never filters."""
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        for parameter, source in operation.parameter_sources.items():
            if source == "scope":
                assert parameter in {"uid", "Uid"}, (operation_id, parameter)
            if source == "credential":
                assert parameter in {"app_key", "timestamp", "sign"}, (operation_id, parameter)


def test_credential_sourced_params_are_never_filters_or_model_output() -> None:
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        for parameter, source in operation.parameter_sources.items():
            if parameter in {"app_key", "timestamp", "sign"}:
                assert source == "credential", (operation_id, parameter)


def test_malformed_catalog_fails_closed(tmp_path: Path) -> None:
    bad_document = {
        "version": 1,
        "operations": [
            {
                "operation_id": "X1_bad",
                "path": "/v1/bad",
                "kind": "resource",
                "parameter_sources": {"tenant_id": "user_text"},
                "pagination": "none",
                "timeout_seconds": 10.0,
            }
        ],
    }
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(bad_document), encoding="utf-8")
    from factory_agent.domain.errors import InvalidRequestError

    with pytest.raises(InvalidRequestError):
        load_catalog(path)


def test_duplicate_operation_ids_fail_startup(tmp_path: Path) -> None:
    from typing import Any

    from factory_agent.domain.errors import InvalidRequestError

    entry: dict[str, Any] = {
        "operation_id": "X1_dup",
        "path": "/v1/dup",
        "kind": "identity",
        "parameter_sources": {"app_key": "credential"},
        "pagination": "none",
        "timeout_seconds": 10.0,
    }
    document: dict[str, Any] = {"version": 1, "operations": [entry, dict(entry)]}
    path = tmp_path / "dup.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(InvalidRequestError):
        load_catalog(path)


def test_missing_catalog_file_fails_startup(tmp_path: Path) -> None:
    from factory_agent.domain.errors import InvalidRequestError

    with pytest.raises(InvalidRequestError):
        load_catalog(tmp_path / "missing.yaml")
