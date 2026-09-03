from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

from factory_agent.data_api.catalog import load_catalog
from factory_agent.domain.errors import InvalidRequestError
from factory_agent.execution.recipes import (
    CapabilityRecipe,
    RecipeStep,
    load_recipes,
    validate_recipe,
)


def _operations() -> frozenset[str]:
    return load_catalog().operation_ids


def test_smoke_recipe_loads_against_catalog() -> None:
    registry = load_recipes(_operations())
    assert "smoke_piecework_summary" in registry
    recipe = registry.get("smoke_piecework_summary")
    # The smoke recipe runs against the customer EmployeeQuery/DeptQuery surfaces.
    assert recipe.metric_versions["output_personal"] == "customer-output-v1"
    assert recipe.metric_versions["org_headcount"] == "employee-registered-v1"
    api_operations = {step.operation_id for step in recipe.steps if step.kind == "api"}
    assert api_operations <= _operations()


def test_recipe_referencing_unregistered_api_fails_startup(tmp_path: Path) -> None:
    document: dict[str, Any] = {
        "version": 1,
        "capabilities": [
            {
                "capability_id": "bad_api_ref",
                "title": "Bad",
                "steps": [
                    {"step_id": "s1", "kind": "api", "operation_id": "X9_unknown"},
                ],
                "result_columns": [],
                "metric_versions": {},
            }
        ],
    }
    path = tmp_path / "bad.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(InvalidRequestError):
        load_recipes(_operations(), directory=tmp_path)


def test_recipe_with_circular_dependency_fails_startup(tmp_path: Path) -> None:
    document: dict[str, Any] = {
        "version": 1,
        "capabilities": [
            {
                "capability_id": "cyclic",
                "title": "Cyclic",
                "steps": [
                    {"step_id": "a", "kind": "local", "depends_on": ["b"], "compute": "x"},
                    {"step_id": "b", "kind": "local", "depends_on": ["a"], "compute": "y"},
                ],
                "result_columns": [],
                "metric_versions": {},
            }
        ],
    }
    path = tmp_path / "cyclic.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(InvalidRequestError):
        load_recipes(_operations(), directory=tmp_path)


def test_recipe_with_unversioned_metric_fails_startup(tmp_path: Path) -> None:
    document: dict[str, Any] = {
        "version": 1,
        "capabilities": [
            {
                "capability_id": "no_version",
                "title": "No version",
                "steps": [
                    {"step_id": "s1", "kind": "api", "operation_id": "YskQuery"},
                ],
                "result_columns": [
                    {"name": "col", "source_step": "s1", "metric": "unknown_metric"}
                ],
                "metric_versions": {},
            }
        ],
    }
    path = tmp_path / "noversion.yaml"
    path.write_text(yaml.safe_dump(document), encoding="utf-8")
    with pytest.raises(InvalidRequestError):
        load_recipes(_operations(), directory=tmp_path)


def test_recipe_with_unknown_dependency_fails_validation() -> None:
    recipe = CapabilityRecipe(
        capability_id="bad_dep",
        title="Bad dependency",
        steps=(
            RecipeStep(step_id="s1", kind="api", operation_id="YskQuery"),
            RecipeStep(step_id="s2", kind="local", depends_on=("ghost",), compute="x"),
        ),
        result_columns=(),
        metric_versions={},
    )
    with pytest.raises(InvalidRequestError):
        validate_recipe(recipe, _operations())


def test_empty_recipe_directory_yields_empty_registry(tmp_path: Path) -> None:
    registry = load_recipes(_operations(), directory=tmp_path)
    assert registry.capability_ids == frozenset()
