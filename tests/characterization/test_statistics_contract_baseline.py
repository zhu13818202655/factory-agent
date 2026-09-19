"""The consolidated statistics surface preserves the pre-merge contract.

``usage-admin`` was a separate FastAPI service served under ``/admin/v1`` until it
was merged into this application (ADR-0003). ``statistics_contract_baseline.json``
is a frozen snapshot of that service's OpenAPI shape, taken from git history while
the package still existed — nothing here imports ``usage_admin``.

The merge is a *controlled* contract change. Exactly four differences are
declared in the baseline, and every other path, parameter, request media type,
status code, and response field has to match. A fifth difference showing up in
these assertions means the statistics API drifted from the contract the front end
was handed, which is the failure this file exists to catch.

The four, in one line each: the ``/admin/v1`` prefix became ``/v1/statistics``;
the path parameter ``{app_key}`` became ``{tenant_ref}``; ``RegistryItemOut``
gained ``tenant_ref``; and the usage board's factory selector stopped handing out
plaintext AppKeys — it returns ``{tenant_ref, tenant_name}`` and the exact-factory
filter moved from ``app_key`` to ``tenant_ref``.
"""

import json
from pathlib import Path
from typing import Any, cast

from fastapi import FastAPI

from factory_agent.statistics.api.router import statistics_router

BASELINE: dict[str, Any] = json.loads(
    (Path(__file__).parent / "statistics_contract_baseline.json").read_text(encoding="utf-8")
)

CHANGES: dict[str, Any] = BASELINE["intended_changes"]
_RETIRED_PATHS: list[str] = BASELINE["retired_paths"]
_RETIRED_SCHEMAS: list[str] = BASELINE["retired_schemas"]

_PREFIX_FROM: str = CHANGES["prefix"]["from"]
_PREFIX_TO: str = CHANGES["prefix"]["to"]
_PARAM_FROM: str = CHANGES["path_param"]["from"]
_PARAM_TO: str = CHANGES["path_param"]["to"]
_PARAM_NAME_FROM: str = _PARAM_FROM.strip("{}")
_PARAM_NAME_TO: str = _PARAM_TO.strip("{}")
_ADDED_FIELD_MODEL: str = CHANGES["response_field_added"]["model"]
_ADDED_FIELD_NAME: str = CHANGES["response_field_added"]["field"]
_ADDED_PARAM: str = CHANGES["query_param_added"]["param"]
# The 4th intended change is one logical move — the factory filter goes through the
# non-secret `tenant_ref` — applied to every aggregate endpoint, not just by-tenant.
_ADDED_PARAM_PATHS: frozenset[str] = frozenset(CHANGES["query_param_added"]["endpoints"])
_SELECTOR_PATH: str = CHANGES["response_model_changed"]["path"]
_SELECTOR_METHOD: str = CHANGES["response_model_changed"]["method"]
_SELECTOR_MODEL: str = CHANGES["response_model_changed"]["model"]
_SELECTOR_FIELDS: list[str] = CHANGES["response_model_changed"]["fields"]

_OPERATIONS = frozenset({"get", "post", "patch", "delete", "put"})


def _operation_shape(operation: dict[str, Any]) -> dict[str, list[str]]:
    parameters = cast(list[dict[str, Any]], operation.get("parameters") or [])
    request_body = cast(dict[str, Any], operation.get("requestBody") or {})
    responses = cast(dict[str, Any], operation.get("responses") or {})
    content = cast(dict[str, Any], request_body.get("content") or {})
    return {
        "params": sorted(
            f"{parameter['in']}:{parameter['name']}"
            + (" required" if parameter.get("required") else "")
            for parameter in parameters
        ),
        "request_media": sorted(content),
        "statuses": sorted(responses),
    }


def _shape(spec: dict[str, Any]) -> dict[str, Any]:
    raw_paths = cast(dict[str, Any], spec["paths"])
    paths: dict[str, dict[str, dict[str, list[str]]]] = {}
    for path, raw_item in raw_paths.items():
        item = cast(dict[str, Any], raw_item)
        paths[path] = {
            method: _operation_shape(cast(dict[str, Any], raw_operation))
            for method, raw_operation in item.items()
            if method in _OPERATIONS
        }

    components = cast(dict[str, Any], spec.get("components") or {})
    raw_schemas = cast(dict[str, Any], components.get("schemas") or {})
    schemas: dict[str, list[str]] = {}
    for name, raw_body in raw_schemas.items():
        properties = cast(dict[str, Any], cast(dict[str, Any], raw_body).get("properties") or {})
        if properties:
            schemas[name] = sorted(properties)
    return {"paths": paths, "schemas": schemas}


def _live_shape() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(statistics_router)
    return _shape(app.openapi())


def _renamed(path: str) -> str:
    """Apply the two declared address changes to a pre-merge path."""
    return path.replace(_PREFIX_FROM, _PREFIX_TO).replace(_PARAM_FROM, _PARAM_TO)


def _renamed_ops(ops: dict[str, dict[str, list[str]]]) -> dict[str, dict[str, list[str]]]:
    return {
        method: {key: [_renamed_param(item) for item in values] for key, values in shape.items()}
        for method, shape in ops.items()
    }


def _renamed_param(parameter: str) -> str:
    """Rename the *path* parameter only.

    ``app_key`` is also a query parameter, and there the handover did not simply
    rename it: the plaintext-key filter is gone (any value is rejected) and a new
    ``tenant_ref`` filter was added beside it. That is why it is declared as
    ``query_param_added`` and handled in :func:`_declared_additions` rather than
    silently rewritten here — a blind rename would have masked the fact that the
    old parameter is still in the signature.
    """
    if parameter.startswith(f"path:{_PARAM_NAME_FROM}"):
        return parameter.replace(_PARAM_NAME_FROM, _PARAM_NAME_TO, 1)
    return parameter


def _declared_additions(target: str, method: str) -> set[str]:
    """Parameters the baseline did not have and the handover deliberately added."""
    if method == "get" and target in _ADDED_PARAM_PATHS:
        return {_ADDED_PARAM}
    return set()


def test_the_declared_changes_are_the_literal_ones_the_handover_documents() -> None:
    assert (_PREFIX_FROM, _PREFIX_TO) == ("/admin/v1", "/v1/statistics")
    assert (_PARAM_NAME_FROM, _PARAM_NAME_TO) == ("app_key", "tenant_ref")
    assert (_ADDED_FIELD_MODEL, _ADDED_FIELD_NAME) == ("RegistryItemOut", "tenant_ref")
    assert _ADDED_PARAM == "query:tenant_ref"
    assert sorted(_ADDED_PARAM_PATHS) == [
        "/v1/statistics/usage/by-tenant",
        "/v1/statistics/usage/capabilities",
        "/v1/statistics/usage/dimensions",
        "/v1/statistics/usage/errors",
        "/v1/statistics/usage/mes-categories",
        "/v1/statistics/usage/mes-failures",
        "/v1/statistics/usage/mes-operations",
        "/v1/statistics/usage/models",
        "/v1/statistics/usage/summary",
        "/v1/statistics/usage/timeseries",
        "/v1/statistics/usage/users",
    ]
    assert (_SELECTOR_PATH, _SELECTOR_METHOD, _SELECTOR_MODEL) == (
        "/v1/statistics/tenants",
        "get",
        "TenantOptionView",
    )
    assert sorted(_SELECTOR_FIELDS) == ["tenant_name", "tenant_ref"]


def test_no_pre_merge_path_disappears_except_the_retired_health_checks() -> None:
    baseline_paths = {_renamed(path) for path in BASELINE["paths"]}
    live_paths = set(_live_shape()["paths"])

    assert sorted(baseline_paths - live_paths) == sorted(_RETIRED_PATHS)
    # The merged service keeps one health pair for everything, so the second set
    # is not replicated. Retiring them is the only removal; nothing else vanished.
    assert set(_RETIRED_PATHS) & live_paths == set()


def test_every_surviving_path_keeps_its_params_media_types_and_statuses() -> None:
    live = _live_shape()["paths"]
    drift: list[str] = []

    for path, ops in BASELINE["paths"].items():
        target = _renamed(path)
        if target in _RETIRED_PATHS:
            continue
        expected = _renamed_ops(ops)
        actual = live.get(target)
        if actual is None:
            drift.append(f"{target}: missing")
            continue
        for method, expected_shape in expected.items():
            actual_shape = actual.get(method)
            if actual_shape is None:
                drift.append(f"{target} {method}: missing")
                continue
            # Only the declared additions may appear; anything else is drift.
            params = sorted(set(actual_shape["params"]) - _declared_additions(target, method))
            normalized = {**actual_shape, "params": params}
            if expected_shape != normalized:
                drift.append(f"{target} {method}: expected {expected_shape} but got {normalized}")

    assert drift == []


def test_no_response_model_field_changes_except_the_tenant_ref_handle() -> None:
    live = _live_shape()["schemas"]
    drift: list[str] = []
    added: list[str] = []

    for name, fields in BASELINE["schemas"].items():
        if name in _RETIRED_SCHEMAS:
            assert name not in live, f"{name}: retired schema came back"
            continue
        if name not in live:
            drift.append(f"{name}: model removed")
            continue
        removed = sorted(set(fields) - set(live[name]))
        extra = sorted(set(live[name]) - set(fields))
        if removed:
            drift.append(f"{name}: removed {removed}")
        if extra:
            added.append(f"{name}: added {extra}")

    assert drift == []
    assert added == [f"{_ADDED_FIELD_MODEL}: added ['{_ADDED_FIELD_NAME}']"]


def test_the_factory_selector_returns_handles_and_names_only() -> None:
    """The usage board's factory picker stopped being a key-distribution channel.

    ``GET /v1/statistics/tenants`` used to answer with the plaintext AppKeys of
    every tenant that had data in the window; the dashboard then rendered them as
    dropdown labels. It now answers with the non-secret ``tenant_ref`` and the
    factory name, which is all the selector ever needed.
    """
    live = _live_shape()

    assert _SELECTOR_MODEL not in BASELINE["schemas"]  # a new model, not a rename
    assert live["schemas"][_SELECTOR_MODEL] == sorted(_SELECTOR_FIELDS)
    assert [field for field in live["schemas"][_SELECTOR_MODEL] if "key" in field] == []


def test_no_response_model_appears_from_nowhere() -> None:
    live = _live_shape()

    new_models = sorted(set(live["schemas"]) - set(BASELINE["schemas"]))

    assert new_models == [_SELECTOR_MODEL]


def test_the_renamed_addresses_are_fully_gone_from_the_live_routes() -> None:
    live = _live_shape()

    assert [path for path in live["paths"] if _PREFIX_FROM in path] == []
    assert [path for path in live["paths"] if _PARAM_FROM in path] == []
    assert [path for path in live["paths"] if _PREFIX_TO not in path] == []
