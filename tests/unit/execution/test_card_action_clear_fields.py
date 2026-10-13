import pytest
from pydantic import ValidationError

from factory_agent.execution.recipes import CardActionSpec


def test_reviewed_action_parses_business_clears_and_projects_metadata() -> None:
    spec = CardActionSpec.model_validate(
        {
            "label": "查看工序",
            "capability_id": "fr005_order_worktype_detail",
            "bind_from": {"material_ids": "material_id"},
            "clear_fields": ["order_codes"],
        }
    )
    assert spec.clear_fields == ("order_codes",)
    assert spec.to_spec().payload() == {
        "type": "drill",
        "label": "查看工序",
        "capability_id": "fr005_order_worktype_detail",
        "bind_from": {"material_ids": "material_id"},
        "clear_fields": ["order_codes"],
    }


@pytest.mark.parametrize(
    "name",
    [
        "dept_ids",
        "employee_uid",
        "employee_ids",
        "tenant_id",
        "user_id",
        "order_view",
    ],
)
def test_reviewed_action_rejects_clearing_scope_or_identity(name: str) -> None:
    with pytest.raises(ValidationError, match="only clear approved business fields"):
        CardActionSpec.model_validate(
            {
                "label": "动作",
                "capability_id": "fr005_order_worktype_detail",
                "clear_fields": [name],
            }
        )


def test_action_with_no_clear_fields_keeps_existing_payload_shape() -> None:
    action = CardActionSpec(label="查看包", capability_id="fr005_order_package_detail")
    assert action.to_spec().payload() == {
        "type": "drill",
        "label": "查看包",
        "capability_id": "fr005_order_package_detail",
        "bind_from": {},
    }
