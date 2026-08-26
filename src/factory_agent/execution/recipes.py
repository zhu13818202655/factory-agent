"""L1 capability recipes: reviewed deterministic DAGs over catalog operations.

Recipe files live under ``configs/knowledge/`` and pass the same strict
validation as the API Catalog. A recipe declares required slots, API steps
with dependencies and parallel groups, local computation, result columns,
metric versions, and degradation rules. Unreviewed recipes can never register.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from factory_agent.domain.errors import InvalidRequestError

DEFAULT_RECIPE_DIR = Path("configs/knowledge/recipes")

StepKind = Literal["api", "local"]
ColumnType = Literal["money", "percent", "date", "quantity"]


class RecipeStep(BaseModel):
    """One node of a capability DAG.

    ``params`` holds reviewed, static filter parameters for the step (e.g. a
    wage ``scheme`` or a fixed ``Type``). They are always ``filter``-sourced in
    the catalog and can never carry scope or credential identifiers.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    step_id: str
    kind: StepKind
    operation_id: str | None = None
    depends_on: tuple[str, ...] = ()
    parallel_group: str | None = None
    optional: bool = False
    compute: str | None = None
    params: dict[str, str] | None = None


class ResultColumn(BaseModel):
    """One output column with optional type/unit for rendering (Story 6)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    source_step: str
    metric: str | None = None
    column_type: ColumnType | None = None
    unit: str | None = None


class CapabilityRecipe(BaseModel):
    """A reviewed L1 capability definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    title: str
    required_slots: tuple[str, ...] = ()
    steps: tuple[RecipeStep, ...]
    result_columns: tuple[ResultColumn, ...]
    metric_versions: dict[str, str]
    degradation: Literal["incomplete_marker", "fail"] = "incomplete_marker"
    #: Optional footer reconciliation: ``{result_column: footer_field}``. The
    #: kernel compares the locally computed column against the MES ``footer``
    #: field; a mismatch produces a structured ``reconciliation_failed`` state
    #: instead of silently picking one number.
    footer_reconciliation: dict[str, str] | None = None


class RecipeDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int
    capabilities: tuple[CapabilityRecipe, ...]


@dataclass(frozen=True, slots=True)
class RecipeRegistry:
    """Immutable runtime registry of validated capability recipes."""

    version: int
    _recipes: dict[str, CapabilityRecipe]

    def get(self, capability_id: str) -> CapabilityRecipe:
        try:
            return self._recipes[capability_id]
        except KeyError as error:
            raise InvalidRequestError(
                f"capability recipe is not registered: {capability_id}"
            ) from error

    def __contains__(self, capability_id: object) -> bool:
        return isinstance(capability_id, str) and capability_id in self._recipes

    @property
    def capability_ids(self) -> frozenset[str]:
        return frozenset(self._recipes)


def validate_recipe(recipe: CapabilityRecipe, registered_operations: frozenset[str]) -> None:
    """Structural validation: references, cycles, and metric versions."""
    step_ids = {step.step_id for step in recipe.steps}
    if len(step_ids) != len(recipe.steps):
        raise InvalidRequestError("recipe contains duplicate step IDs")

    for step in recipe.steps:
        if step.kind == "api":
            if step.operation_id is None or step.operation_id not in registered_operations:
                raise InvalidRequestError(f"step {step.step_id} references an unregistered API")
        elif step.compute is None:
            raise InvalidRequestError(f"local step {step.step_id} requires a compute rule")
        for dependency in step.depends_on:
            if dependency not in step_ids:
                raise InvalidRequestError(
                    f"step {step.step_id} depends on unknown step {dependency}"
                )

    _reject_cycles(recipe)
    for column in recipe.result_columns:
        if column.source_step not in step_ids:
            raise InvalidRequestError(
                f"result column {column.name} references unknown step {column.source_step}"
            )
        if column.metric is not None and column.metric not in recipe.metric_versions:
            raise InvalidRequestError(
                f"result column {column.name} uses a metric without a version"
            )


def _reject_cycles(recipe: CapabilityRecipe) -> None:
    edges = {step.step_id: step.depends_on for step in recipe.steps}
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(node: str) -> None:
        if node in visiting:
            raise InvalidRequestError("recipe contains a circular dependency")
        if node in visited:
            return
        visiting.add(node)
        for dependency in edges.get(node, ()):
            visit(dependency)
        visiting.discard(node)
        visited.add(node)

    for step_id in edges:
        visit(step_id)


def load_recipes(
    registered_operations: frozenset[str],
    directory: Path | None = None,
) -> RecipeRegistry:
    """Load all reviewed recipe files; any failure blocks startup."""
    recipe_dir = directory or DEFAULT_RECIPE_DIR
    recipes: dict[str, CapabilityRecipe] = {}
    version = 1

    files = sorted(recipe_dir.glob("*.yaml")) if recipe_dir.exists() else []
    for path in files:
        try:
            raw = yaml.safe_load(path.read_text(encoding="utf-8"))
        except (OSError, yaml.YAMLError) as error:
            raise InvalidRequestError(f"recipe file is unreadable: {path.name}") from error
        try:
            document = RecipeDocument.model_validate(raw)
        except ValidationError as error:
            raise InvalidRequestError(f"recipe failed schema validation: {path.name}") from error

        version = document.version
        for recipe in document.capabilities:
            if recipe.capability_id in recipes:
                raise InvalidRequestError("duplicate capability ID across recipe files")
            validate_recipe(recipe, registered_operations)
            recipes[recipe.capability_id] = recipe

    return RecipeRegistry(version=version, _recipes=recipes)


__all__ = [
    "CapabilityRecipe",
    "DEFAULT_RECIPE_DIR",
    "RecipeDocument",
    "RecipeRegistry",
    "ResultColumn",
    "RecipeStep",
    "load_recipes",
    "validate_recipe",
]
