"""L1 capability recipes: reviewed deterministic DAGs over catalog operations.

Recipe files live under ``configs/knowledge/`` and pass the same strict
validation as the API Catalog. A recipe declares required slots, API steps
with dependencies and parallel groups, local computation, result columns,
metric versions, and degradation rules. Unreviewed recipes can never register.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import yaml
from pydantic import BaseModel, ConfigDict, ValidationError

from factory_agent.domain.errors import InvalidRequestError
from factory_agent.ports.card import (
    MAX_PREVIEW_GROUPS,
    MAX_PREVIEW_ROWS,
    CardAction,
    CardAlertSpec,
    CardTableSpec,
)

DEFAULT_RECIPE_DIR = Path("configs/knowledge/recipes")

StepKind = Literal["api", "local"]
ColumnType = Literal["money", "percent", "date", "quantity"]
CardKind = Literal["kpi", "table", "ranking"]

#: Only typed numeric columns may appear in the KPI area, so a uid-like string
#: column can never be presented as a figure.
CARD_METRIC_TYPES: frozenset[str] = frozenset({"money", "quantity", "percent"})

#: Business filter keys a recipe may bind into local compute. They narrow
#: within the MES-filtered range and can never broaden the active DataScope.
#: ``requested_dept_ids`` is the user-requested department intersection (None
#: when the user did not restrict to one), distinct from the full scope depts.
#: ``material_ids`` is the package material number set that opens the
#: package-to-worktype drill-down.
BUSINESS_FILTER_KEYS: frozenset[str] = frozenset(
    {"order_codes", "style_codes", "plan_codes", "material_ids", "requested_dept_ids"}
)


class ParamBinding(BaseModel):
    """Derive one API request parameter from a dependency step's rows.

    The kernel resolves the distinct values of ``column`` in the sandbox table
    registered by ``from_step`` and fans out one API call per value (e.g.
    ``WorktypeProgressQuery.userid`` = each ``Sclzd.id`` material number).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    from_step: str
    column: str


class RecipeStep(BaseModel):
    """One node of a capability DAG.

    ``params`` holds reviewed, static filter parameters for the step (e.g. a
    wage ``scheme`` or a fixed ``Type``). They are always ``filter``-sourced in
    the catalog and can never carry scope or credential identifiers.

    ``param_bindings`` derives dynamic filter parameters from a dependency
    step's rows (fan-out); ``filter_bindings`` binds reviewed business filters
    from the narrowed request into the local compute as named parameters.
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
    param_bindings: dict[str, ParamBinding] | None = None
    filter_bindings: dict[str, str] | None = None


class ResultColumn(BaseModel):
    """One output column with optional display title, type, and unit.

    ``name`` is the stable identifier (SQL column, totals key, tests);
    ``title`` is the worker-facing Chinese label used by the card, the XLSX
    header, and the composed answer.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    source_step: str
    title: str | None = None
    metric: str | None = None
    column_type: ColumnType | None = None
    unit: str | None = None


class CardAlertMarker(BaseModel):
    """Reviewed highlight rule: rows where ``column == equals`` (client-side styling)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    column: str
    equals: str


#: Drill slot keys a card action may bind (keys of the follow-up ``drill``
#: request payload). They are business narrowing keys only; scope identifiers
#: (employee/dept *scope* sets, tenant, user) are absent by design and are
#: re-validated server-side on every drill round.
ALLOWED_DRILL_SLOT_KEYS: frozenset[str] = frozenset({"dept_ids", "employee_uid"})


class CardActionSpec(BaseModel):
    """Reviewed card-level drill entry (D4-3/D4-4 拍板).

    ``bind_from`` maps drill slot keys to result column names; the follow-up
    drill request's slots are filled from the clicked row at the client.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    capability_id: str
    bind_from: dict[str, str] = {}

    def to_spec(self) -> CardAction:
        return CardAction(
            label=self.label,
            capability_id=self.capability_id,
            bind_from=dict(self.bind_from),
        )


class AuxMetric(BaseModel):
    """One card-only KPI produced by an aux output (不可 SUM 的单值口径)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    title: str
    unit: str | None = None


class AuxOutput(BaseModel):
    """A secondary compute output that feeds the card only (§4.2 of the plan).

    Aux rows never enter the main result table or the export file; they carry
    the chart series and the factory-level single-value KPIs the main table's
    SUM-totals cannot express. Row count stays bounded: a chart longer than
    ``MAX_CHART_DAY_POINTS`` is downsampled to weeks by the card builder.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    aux_id: str
    into: Literal["chart", "metrics"]
    compute: str
    depends_on: tuple[str, ...] = ()
    #: ``into=chart`` declaration (D4-1/D4-2 拍板).
    chart_type: Literal["bar"] = "bar"
    chart_unit: str | None = None
    label_column: str = "label"
    value_column: str = "value"
    #: ``into=metrics`` declaration: one entry per KPI column of the output.
    metrics: tuple[AuxMetric, ...] = ()


class CardSpec(BaseModel):
    """Reviewed result-card shape for one capability.

    ``kind=kpi`` renders a KPI area only; ``table``/``ranking`` add a preview
    table (``ranking`` requires ``rank_column``). ``preview_max_rows`` is the
    per-group cap on grouped cards and is mandatory for any table-bearing card.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: CardKind
    metrics: tuple[str, ...] = ()
    totals: tuple[str, ...] = ()
    preview_max_rows: int | None = None
    preview_max_groups: int = 50
    group_by: str | None = None
    #: Result column supplying the group display name (groups[].name).
    group_name_from: str | None = None
    rank_column: str | None = None
    alert_marker: CardAlertMarker | None = None
    #: Reviewed card-level drill entry points.
    actions: tuple[CardActionSpec, ...] = ()
    #: Reviewed static 口径 statements rendered with the card. Mandatory when a
    #: column's meaning is not self-evident (e.g. a 件·工序 figure that must not
    #: be read as a piece count).
    notes: tuple[str, ...] = ()

    def to_table_spec(self) -> CardTableSpec:
        """Runtime projection for the ports-layer card builder."""
        return CardTableSpec(
            kind=self.kind,
            metrics=self.metrics,
            totals=self.totals,
            preview_max_rows=self.preview_max_rows,
            preview_max_groups=self.preview_max_groups,
            group_by=self.group_by,
            group_name_from=self.group_name_from,
            rank_column=self.rank_column,
            alert_marker=(
                CardAlertSpec(column=self.alert_marker.column, equals=self.alert_marker.equals)
                if self.alert_marker is not None
                else None
            ),
            actions=tuple(action.to_spec() for action in self.actions),
            notes=self.notes,
        )


class CapabilityRecipe(BaseModel):
    """A reviewed L1 capability definition."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    capability_id: str
    title: str
    required_slots: tuple[str, ...] = ()
    steps: tuple[RecipeStep, ...]
    result_columns: tuple[ResultColumn, ...]
    metric_versions: dict[str, str]
    #: Optional front-end card declaration; absent = no card is emitted.
    card: CardSpec | None = None
    #: Optional card-only auxiliary outputs (chart series / single-value KPIs).
    aux_outputs: tuple[AuxOutput, ...] = ()
    degradation: Literal["incomplete_marker", "fail"] = "incomplete_marker"
    #: Optional footer reconciliation: ``{result_column: footer_field}``. The
    #: kernel compares the locally computed column against the MES ``footer``
    #: field and logs any mismatch as a warning; the customer footer is trusted
    #: as authoritative and the comparison never changes the result.
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

    api_step_ids = {step.step_id for step in recipe.steps if step.kind == "api"}
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
        for param, binding in (step.param_bindings or {}).items():
            if binding.from_step not in api_step_ids:
                raise InvalidRequestError(
                    f"step {step.step_id} param {param} binds from a non-API step"
                )
            if not _is_safe_identifier(binding.from_step) or not _is_safe_identifier(
                binding.column
            ):
                raise InvalidRequestError(
                    f"step {step.step_id} param {param} binds an unsafe identifier"
                )
        for _, filter_key in (step.filter_bindings or {}).items():
            if filter_key not in BUSINESS_FILTER_KEYS:
                raise InvalidRequestError(
                    f"step {step.step_id} binds an unknown business filter {filter_key}"
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
    if recipe.card is not None:
        _validate_card(recipe)
    _validate_aux_outputs(recipe, api_step_ids)


def _validate_aux_outputs(recipe: CapabilityRecipe, api_step_ids: set[str]) -> None:
    """Aux outputs must reference known API steps and stay internally coherent."""
    aux_ids = {aux.aux_id for aux in recipe.aux_outputs}
    if len(aux_ids) != len(recipe.aux_outputs):
        raise InvalidRequestError("recipe contains duplicate aux output IDs")
    for aux in recipe.aux_outputs:
        for dependency in aux.depends_on:
            if dependency not in api_step_ids:
                raise InvalidRequestError(
                    f"aux output {aux.aux_id} depends on unknown API step {dependency}"
                )
        if not aux.compute.strip():
            raise InvalidRequestError(f"aux output {aux.aux_id} requires a compute rule")
        if aux.into == "chart":
            if aux.chart_unit is None:
                raise InvalidRequestError(
                    f"aux output {aux.aux_id} declares a chart without chart_unit"
                )
        elif not aux.metrics:
            raise InvalidRequestError(f"aux output {aux.aux_id} declares metrics without entries")


def _validate_card(recipe: CapabilityRecipe) -> None:
    """Card declaration must reference existing columns and stay bounded."""
    card = recipe.card
    assert card is not None  # caller guarantees
    columns_by_name = {column.name: column for column in recipe.result_columns}

    references = (*card.metrics, *card.totals)
    if card.group_by is not None:
        references += (card.group_by,)
    if card.rank_column is not None:
        references += (card.rank_column,)
    if card.alert_marker is not None:
        references += (card.alert_marker.column,)
    if card.group_name_from is not None:
        references += (card.group_name_from,)
    for name in references:
        if name not in columns_by_name:
            raise InvalidRequestError(f"card references unknown result column {name}")

    if card.kind == "kpi":
        if not card.metrics:
            raise InvalidRequestError("kpi card requires at least one metric")
        has_table_keys = (
            card.group_by is not None
            or card.rank_column is not None
            or card.alert_marker is not None
        )
        if has_table_keys:
            raise InvalidRequestError("kpi card cannot declare a preview table")
    else:
        if card.preview_max_rows is None:
            raise InvalidRequestError("table-bearing card requires preview_max_rows")
        if not 1 <= card.preview_max_rows <= MAX_PREVIEW_ROWS:
            raise InvalidRequestError(f"card preview_max_rows must be within 1..{MAX_PREVIEW_ROWS}")
        if card.kind == "ranking" and card.rank_column is None:
            raise InvalidRequestError("ranking card requires rank_column")

    if not 1 <= card.preview_max_groups <= MAX_PREVIEW_GROUPS:
        raise InvalidRequestError(f"card preview_max_groups must be within 1..{MAX_PREVIEW_GROUPS}")

    for name in card.metrics:
        if columns_by_name[name].column_type not in CARD_METRIC_TYPES:
            raise InvalidRequestError(
                f"card metric {name} must be a numeric column ({sorted(CARD_METRIC_TYPES)})"
            )

    for action in card.actions:
        if not _is_safe_identifier(action.capability_id):
            raise InvalidRequestError("card action capability_id must be a plain identifier")
        for slot_key, column_name in action.bind_from.items():
            if slot_key not in ALLOWED_DRILL_SLOT_KEYS:
                raise InvalidRequestError(f"card action binds an unknown drill slot {slot_key}")
            if column_name not in columns_by_name:
                raise InvalidRequestError(f"card action binds unknown result column {column_name}")


def _is_safe_identifier(value: str) -> bool:
    """Identifiers used in sandbox SQL must be plain names only."""
    if not value:
        return False
    first = value[0]
    rest = value[1:]
    return (first.isalpha() or first == "_") and all(char.isalnum() or char == "_" for char in rest)


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

    _validate_card_action_targets(recipes)
    return RecipeRegistry(version=version, _recipes=recipes)


def _validate_card_action_targets(recipes: dict[str, CapabilityRecipe]) -> None:
    """Every card drill action must target a registered capability recipe."""
    for recipe in recipes.values():
        if recipe.card is None:
            continue
        for action in recipe.card.actions:
            if action.capability_id not in recipes:
                raise InvalidRequestError(
                    f"card action of {recipe.capability_id} targets unregistered "
                    f"capability {action.capability_id}"
                )


__all__ = [
    "ALLOWED_DRILL_SLOT_KEYS",
    "AuxMetric",
    "AuxOutput",
    "BUSINESS_FILTER_KEYS",
    "CapabilityRecipe",
    "CardActionSpec",
    "CardAlertMarker",
    "CardSpec",
    "DEFAULT_RECIPE_DIR",
    "ParamBinding",
    "RecipeDocument",
    "RecipeRegistry",
    "ResultColumn",
    "RecipeStep",
    "load_recipes",
    "validate_recipe",
]
