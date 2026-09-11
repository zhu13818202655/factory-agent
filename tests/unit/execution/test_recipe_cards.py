"""Recipe card-declaration validation: bad cards block startup."""

from pathlib import Path
from typing import Any

import pytest
import yaml

from factory_agent.data_api.catalog import load_catalog
from factory_agent.domain.errors import InvalidRequestError
from factory_agent.execution.recipes import load_recipes


def _operations() -> frozenset[str]:
    return load_catalog().operation_ids


def _recipe_document(card: dict[str, Any]) -> dict[str, Any]:
    """A minimal valid recipe with two result columns (one numeric, one text)."""
    return {
        "version": 1,
        "capabilities": [
            {
                "capability_id": "card_check",
                "title": "Card check",
                "steps": [
                    {"step_id": "s1", "kind": "api", "operation_id": "YskQuery"},
                    {
                        "step_id": "compute",
                        "kind": "local",
                        "depends_on": ["s1"],
                        "compute": "SELECT 1",
                    },
                ],
                "result_columns": [
                    {"name": "amount", "source_step": "compute", "column_type": "money"},
                    {"name": "uname", "source_step": "compute"},
                ],
                "metric_versions": {},
                "card": card,
            }
        ],
    }


def _load(tmp_path: Path, card: dict[str, Any]) -> None:
    path = tmp_path / "card.yaml"
    path.write_text(yaml.safe_dump(_recipe_document(card)), encoding="utf-8")
    load_recipes(_operations(), directory=tmp_path)


def test_valid_kpi_card_loads(tmp_path: Path) -> None:
    _load(tmp_path, {"kind": "kpi", "metrics": ["amount"]})
    registry = load_recipes(_operations(), directory=tmp_path)
    card = registry.get("card_check").card
    assert card is not None
    assert card.kind == "kpi"


def test_valid_grouped_ranking_card_loads(tmp_path: Path) -> None:
    _load(
        tmp_path,
        {
            "kind": "ranking",
            "metrics": ["amount"],
            "preview_max_rows": 10,
            "group_by": "uname",
            "rank_column": "amount",
        },
    )
    registry = load_recipes(_operations(), directory=tmp_path)
    assert registry.get("card_check").card is not None


@pytest.mark.parametrize(
    ("card", "fragment"),
    [
        ({"kind": "kpi"}, "at least one metric"),
        ({"kind": "kpi", "metrics": ["ghost"]}, "unknown result column"),
        (
            {"kind": "kpi", "metrics": ["amount"], "group_by": "uname"},
            "cannot declare a preview table",
        ),
        ({"kind": "table"}, "requires preview_max_rows"),
        ({"kind": "table", "preview_max_rows": 0}, "within 1.."),
        (
            {"kind": "table", "preview_max_rows": 10, "rank_column": "ghost"},
            "unknown result column",
        ),
        ({"kind": "ranking", "preview_max_rows": 10}, "requires rank_column"),
        ({"kind": "table", "preview_max_rows": 10, "metrics": ["uname"]}, "numeric column"),
        (
            {
                "kind": "table",
                "preview_max_rows": 10,
                "alert_marker": {"column": "ghost", "equals": "1"},
            },
            "unknown result column",
        ),
    ],
)
def test_invalid_card_blocks_startup(tmp_path: Path, card: dict[str, Any], fragment: str) -> None:
    with pytest.raises(InvalidRequestError, match=fragment):
        _load(tmp_path, card)


def test_every_business_recipe_declares_a_card_except_smoke() -> None:
    registry = load_recipes(_operations())
    missing = sorted(
        capability_id
        for capability_id in registry.capability_ids
        if registry.get(capability_id).card is None and capability_id != "smoke_piecework_summary"
    )
    assert missing == []
