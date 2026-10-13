from pathlib import Path

import pytest
import yaml

from factory_agent.data_api.catalog import DEFAULT_CATALOG_PATH, load_catalog


def test_default_catalog_loads_and_covers_31_customer_operations() -> None:
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    # 2026-10-11 V2 换代：新增 GongziClQuery（工资产量 V2），30 → 31。
    assert len(catalog.operation_ids) == 31
    assert "SystemToken" in catalog
    assert "YskQuery" in catalog
    assert "GongziMxQuery" in catalog
    assert "MoveMenuQuery" in catalog  # registered but disabled
    assert catalog.get("MoveMenuQuery").enabled is False
    # V2 新主源：工资产量（工资/产量族）与生产进度 V2。
    assert "GongziClQuery" in catalog
    assert catalog.get("GongziClQuery").enabled is True
    assert catalog.get("GongziClQuery").pagination == "none"
    # 缝制进度族（V2 换代后 ScjdQuery 为唯一取数源；V1 明细/分组操作登记
    # 保留但停用，运行时拒绝——见 docs/design/V2接口取数重构与缓存加速方案.md 附录 D）
    for scjd in ("ScjdDetailQuery", "ScjdGxQuery", "ScjdFzHzQuery"):
        assert scjd in catalog, scjd
        assert catalog.get(scjd).enabled is False
    assert catalog.get("ScjdQuery").enabled is True
    assert catalog.get("ScjdQuery").pagination == "none"
    # V2 换代停用的其余 V1 操作（Sclzd 桥、扫码产量、工序进度、工资两个旧源）
    for retired in (
        "SclzdGridPageList",
        "BarcodeClQuery",
        "WorktypeProgressQuery",
        "GongziMxQuery",
        "GongziJeOrderQuery",
    ):
        assert catalog.get(retired).enabled is False, retired
    # 生产计划接口弃用：条目保留（计费分类与种子不变），运行时拒绝调用
    assert catalog.get("PlanGridPageList").enabled is False
    assert "A1_getTenantMembership" not in catalog
    assert "C1_listPieceworkRecords" not in catalog


def test_unknown_operation_is_rejected_at_runtime() -> None:
    from factory_agent.domain.errors import UnsupportedOperationError

    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    with pytest.raises(UnsupportedOperationError):
        catalog.get("X9_notRegistered")


def test_scope_sourced_params_only_carry_data_scope_identifiers() -> None:
    """Scope params originate from authorized employee or department values."""
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        for parameter, source in operation.parameter_sources.items():
            if source == "scope":
                assert parameter in {"uid", "Uid", "dept"}, (operation_id, parameter)
            if source == "credential":
                assert parameter in {"app_key", "timestamp", "sign"}, (operation_id, parameter)


def test_credential_sourced_params_are_never_filters_or_model_output() -> None:
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    for operation_id in catalog.operation_ids:
        operation = catalog.get(operation_id)
        for parameter, source in operation.parameter_sources.items():
            if parameter in {"app_key", "timestamp", "sign"}:
                assert source == "credential", (operation_id, parameter)


def test_payroll_department_is_an_optional_scope_parameter() -> None:
    operation = load_catalog(DEFAULT_CATALOG_PATH).get("GongziClQuery")
    assert operation.parameter_sources["dept"] == "scope"
    assert "dept" not in operation.required_params


def test_catalog_operations_carry_no_wire_body_style() -> None:
    """Business params are flat for every operation (customer contract; real MES
    2026-09-06 confirms wrapped ``param`` bodies are silently ignored across all
    families), so the catalog no longer carries a per-operation body_style."""
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    assert catalog.operation_ids
    for operation_id in catalog.operation_ids:
        assert not hasattr(catalog.get(operation_id), "body_style"), operation_id


def test_query_footer_boolean_params_are_declared_for_footer_operations() -> None:
    """queryFooter must be sent as a real boolean (customer contract 2026-09-06)."""
    catalog = load_catalog(DEFAULT_CATALOG_PATH)
    for operation_id in ("GongziMxQuery", "GongziJeOrderQuery", "HuohaoWtCLQuery"):
        assert catalog.get(operation_id).boolean_params == ("queryFooter",), operation_id
    assert catalog.get("YskQuery").boolean_params == ()


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
