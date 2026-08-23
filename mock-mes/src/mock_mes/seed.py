from __future__ import annotations

import argparse
import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Sequence

import psycopg
from psycopg.types.json import Jsonb

from mock_mes.config import get_settings

Scenario = Literal["small", "standard"]
Record = dict[str, object]
RESOURCE_ID_FIELDS = {
    "organization_assignments": "assignment_id",
    "employees": "employee_id",
    "departments": "dept_id",
    "piecework_records": "record_id",
    "orders": "order_id",
    "styles": "style_id",
    "operations": "operation_id",
    "production_plans": "plan_id",
    "payroll_settlements": "settlement_id",
}


@dataclass(frozen=True, slots=True)
class Dataset:
    scenario: Scenario
    seed: int
    virtual_now: datetime
    memberships_by_subject: dict[str, list[Record]]
    scopes_by_subject: dict[str, dict[str, list[Record]]]
    resources: dict[str, list[Record]]

    def digest(self) -> str:
        payload = {
            "scenario": self.scenario,
            "seed": self.seed,
            "virtual_now": self.virtual_now.isoformat(),
            "memberships": self.memberships_by_subject,
            "scopes": self.scopes_by_subject,
            "resources": self.resources,
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    def piecework_totals(self, tenant_id: str) -> tuple[Decimal, Decimal]:
        records = [
            record
            for record in self.resources["piecework_records"]
            if record["tenant_id"] == tenant_id
        ]
        quantity = sum(
            (Decimal(str(record["completed_quantity"])) for record in records), Decimal()
        )
        amount = sum((Decimal(str(record["amount"])) for record in records), Decimal())
        return quantity, amount


def membership(
    membership_id: str,
    user_id: str,
    tenant_id: str,
    employee_id: str,
    role: str,
    dept_ids: list[str],
) -> Record:
    return {
        "membership_id": membership_id,
        "user_id": user_id,
        "tenant_id": tenant_id,
        "employee_id": employee_id,
        "role": role,
        "dept_ids": dept_ids,
        "valid_from": "2026-01-01T00:00:00Z",
        "valid_to": None,
    }


def build_dataset(
    scenario: Scenario = "small",
    seed: int = 20260821,
    virtual_now: datetime = datetime(2026, 8, 21, 8, tzinfo=timezone.utc),
) -> Dataset:
    memberships = {
        "tenant-a-user": [
            membership(
                "membership-a", "user-a", "tenant-a", "employee-a1", "employee", ["group-a1"]
            )
        ],
        "tenant-b-user": [
            membership(
                "membership-b",
                "user-b",
                "tenant-b",
                "employee-b1",
                "manager",
                ["workshop-b1"],
            )
        ],
        "single-tenant": [
            membership(
                "membership-single",
                "user-single",
                "tenant-a",
                "employee-a2",
                "employee",
                ["group-a1", "group-a2"],
            )
        ],
        "manager-a": [
            membership(
                "membership-manager",
                "user-manager",
                "tenant-a",
                "employee-a9",
                "manager",
                ["workshop-a1"],
            )
        ],
    }
    scopes: dict[str, dict[str, list[Record]]] = {
        "tenant-a-user": {
            "tenant-a": [
                {
                    "scope_id": "scope-a1",
                    "membership_id": "membership-a",
                    "tenant_id": "tenant-a",
                    "employee_ids": ["employee-a1"],
                    "dept_ids": ["group-a1"],
                    "evaluated_at": virtual_now.isoformat().replace("+00:00", "Z"),
                }
            ]
        },
        "tenant-b-user": {
            "tenant-b": [
                {
                    "scope_id": "scope-b1",
                    "membership_id": "membership-b",
                    "tenant_id": "tenant-b",
                    "employee_ids": ["employee-b1"],
                    "dept_ids": ["workshop-b1", "group-b1"],
                    "evaluated_at": virtual_now.isoformat().replace("+00:00", "Z"),
                }
            ]
        },
        "single-tenant": {
            "tenant-a": [
                {
                    "scope_id": "scope-a2",
                    "membership_id": "membership-single",
                    "tenant_id": "tenant-a",
                    "employee_ids": ["employee-a2"],
                    "dept_ids": ["group-a1", "group-a2"],
                    "evaluated_at": virtual_now.isoformat().replace("+00:00", "Z"),
                }
            ]
        },
        "manager-a": {
            "tenant-a": [
                {
                    "scope_id": "scope-manager-a",
                    "membership_id": "membership-manager",
                    "tenant_id": "tenant-a",
                    "employee_ids": ["employee-a1", "employee-a2", "employee-a3", "employee-a9"],
                    "dept_ids": ["workshop-a1", "group-a1", "group-a2"],
                    "evaluated_at": virtual_now.isoformat().replace("+00:00", "Z"),
                }
            ]
        },
    }
    resources: dict[str, list[Record]] = {
        "departments": [
            {
                "dept_id": "factory-a",
                "tenant_id": "tenant-a",
                "parent_id": None,
                "name": "Synthetic Factory A",
                "organization_type": "factory",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "dept_id": "workshop-a1",
                "tenant_id": "tenant-a",
                "parent_id": "factory-a",
                "name": "Synthetic Workshop A",
                "organization_type": "workshop",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "dept_id": "group-a1",
                "tenant_id": "tenant-a",
                "parent_id": "workshop-a1",
                "name": "Synthetic Group One",
                "organization_type": "group",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "dept_id": "group-a2",
                "tenant_id": "tenant-a",
                "parent_id": "workshop-a1",
                "name": "Synthetic Group Two",
                "organization_type": "group",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "dept_id": "workshop-b1",
                "tenant_id": "tenant-b",
                "parent_id": None,
                "name": "Synthetic Workshop B",
                "organization_type": "workshop",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "dept_id": "group-b1",
                "tenant_id": "tenant-b",
                "parent_id": "workshop-b1",
                "name": "Synthetic Group B",
                "organization_type": "group",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
        ],
        "employees": [
            {
                "employee_id": "employee-a1",
                "tenant_id": "tenant-a",
                "employee_number": "SYN-001",
                "display_name": "Same Synthetic Name",
                "dept_ids": ["group-a1"],
                "status": "active",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "employee_id": "employee-a2",
                "tenant_id": "tenant-a",
                "employee_number": "SYN-002",
                "display_name": "Same Synthetic Name",
                "dept_ids": ["group-a1", "group-a2"],
                "status": "active",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "employee_id": "employee-a3",
                "tenant_id": "tenant-a",
                "employee_number": "SYN-003",
                "display_name": "Transferred Worker",
                "dept_ids": ["group-a2"],
                "status": "active",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "employee_id": "employee-a9",
                "tenant_id": "tenant-a",
                "employee_number": "SYN-009",
                "display_name": "Synthetic Manager",
                "dept_ids": ["workshop-a1"],
                "status": "active",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "employee_id": "employee-b1",
                "tenant_id": "tenant-b",
                "employee_number": "SYN-B01",
                "display_name": "Tenant B Worker",
                "dept_ids": ["group-b1"],
                "status": "active",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
        ],
        "organization_assignments": [
            {
                "assignment_id": "assignment-a1",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a1",
                "dept_id": "group-a1",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": None,
            },
            {
                "assignment_id": "assignment-a2-1",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a2",
                "dept_id": "group-a1",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": None,
            },
            {
                "assignment_id": "assignment-a2-2",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a2",
                "dept_id": "group-a2",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": None,
            },
            {
                "assignment_id": "assignment-a3-old",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a3",
                "dept_id": "group-a1",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": "2026-08-15T00:00:00Z",
            },
            {
                "assignment_id": "assignment-a3-new",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a3",
                "dept_id": "group-a2",
                "valid_from": "2026-08-15T00:00:00Z",
                "valid_to": None,
            },
            {
                "assignment_id": "assignment-b1",
                "tenant_id": "tenant-b",
                "employee_id": "employee-b1",
                "dept_id": "group-b1",
                "valid_from": "2026-01-01T00:00:00Z",
                "valid_to": None,
            },
        ],
        "styles": [
            {
                "style_id": "style-a1",
                "tenant_id": "tenant-a",
                "style_number": "SYN-STYLE-001",
                "name": "Synthetic Style A",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
                "status": "active",
            },
            {
                "style_id": "style-b1",
                "tenant_id": "tenant-b",
                "style_number": "SYN-STYLE-B01",
                "name": "Synthetic Style B",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
                "status": "active",
            },
        ],
        "orders": [
            {
                "order_id": "order-a1",
                "tenant_id": "tenant-a",
                "order_number": "SYN-ORDER-001",
                "style_id": "style-a1",
                "responsible_dept_ids": ["group-a1"],
                "ordered_at": "2026-07-01T00:00:00Z",
                "due_at": "2026-08-31T00:00:00Z",
                "ordered_quantity": "100",
                "completed_quantity": "20",
                "status": "in_progress",
            },
            {
                "order_id": "order-delayed",
                "tenant_id": "tenant-a",
                "order_number": "SYN-DELAY-001",
                "style_id": "style-a1",
                "responsible_dept_ids": ["group-a2"],
                "ordered_at": "2026-06-01T00:00:00Z",
                "due_at": "2026-08-01T00:00:00Z",
                "ordered_quantity": "50",
                "completed_quantity": "10",
                "status": "delayed",
            },
            {
                "order_id": "order-b1",
                "tenant_id": "tenant-b",
                "order_number": "SYN-B-001",
                "style_id": "style-b1",
                "responsible_dept_ids": ["group-b1"],
                "ordered_at": "2026-08-01T00:00:00Z",
                "due_at": "2026-09-01T00:00:00Z",
                "ordered_quantity": "20",
                "completed_quantity": "5",
                "status": "in_progress",
            },
        ],
        "operations": [
            {
                "operation_id": "operation-a1",
                "tenant_id": "tenant-a",
                "style_id": "style-a1",
                "order_id": "order-a1",
                "name": "Parallel Operation One",
                "sequence": 1,
                "unit": "piece",
                "unit_rate": "1.2500",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "operation_id": "operation-a2",
                "tenant_id": "tenant-a",
                "style_id": "style-a1",
                "order_id": "order-a1",
                "name": "Parallel Operation Two",
                "sequence": 1,
                "unit": "piece",
                "unit_rate": "0.7500",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
            {
                "operation_id": "operation-b1",
                "tenant_id": "tenant-b",
                "style_id": "style-b1",
                "order_id": "order-b1",
                "name": "Tenant B Operation",
                "sequence": 1,
                "unit": "piece",
                "unit_rate": "1.0000",
                "effective_from": "2026-01-01T00:00:00Z",
                "effective_to": None,
            },
        ],
        "production_plans": [
            {
                "plan_id": "plan-a1",
                "tenant_id": "tenant-a",
                "dept_id": "group-a1",
                "order_id": "order-a1",
                "style_id": "style-a1",
                "starts_at": "2026-08-01T00:00:00Z",
                "ends_at": "2026-08-31T00:00:00Z",
                "planned_quantity": "100",
                "completed_quantity": "20",
                "status": "active",
            },
            {
                "plan_id": "plan-zero",
                "tenant_id": "tenant-a",
                "dept_id": "group-a2",
                "order_id": "order-delayed",
                "style_id": "style-a1",
                "starts_at": "2026-08-01T00:00:00Z",
                "ends_at": "2026-08-31T00:00:00Z",
                "planned_quantity": "0",
                "completed_quantity": "0",
                "status": "delayed",
            },
        ],
        "piecework_records": [
            {
                "record_id": "piece-july",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a1",
                "dept_id": "group-a1",
                "order_id": "order-a1",
                "style_id": "style-a1",
                "operation_id": "operation-a1",
                "plan_id": "plan-a1",
                "work_at": "2026-07-31T23:30:00Z",
                "completed_quantity": "4",
                "qualified_quantity": "4",
                "defective_quantity": "0",
                "unit_rate": "1.2500",
                "amount": "5.0000",
                "status": "settled",
            },
            {
                "record_id": "piece-aug",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a1",
                "dept_id": "group-a1",
                "order_id": "order-a1",
                "style_id": "style-a1",
                "operation_id": "operation-a1",
                "plan_id": "plan-a1",
                "work_at": "2026-08-01T00:30:00Z",
                "completed_quantity": "6",
                "qualified_quantity": "5",
                "defective_quantity": "1",
                "unit_rate": "1.2500",
                "amount": "7.5000",
                "status": "unsettled",
            },
            {
                "record_id": "piece-rework",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a2",
                "dept_id": "group-a2",
                "order_id": "order-a1",
                "style_id": "style-a1",
                "operation_id": "operation-a1",
                "plan_id": "plan-a1",
                "work_at": "2026-08-20T10:00:00Z",
                "completed_quantity": "3",
                "qualified_quantity": "3",
                "defective_quantity": "0",
                "unit_rate": "1.2500",
                "amount": "3.7500",
                "status": "rework",
            },
            {
                "record_id": "piece-parallel",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a2",
                "dept_id": "group-a2",
                "order_id": "order-a1",
                "style_id": "style-a1",
                "operation_id": "operation-a2",
                "plan_id": "plan-a1",
                "work_at": "2026-08-20T10:00:00Z",
                "completed_quantity": "3",
                "qualified_quantity": "3",
                "defective_quantity": "0",
                "unit_rate": "0.7500",
                "amount": "2.2500",
                "status": "reported",
            },
            {
                "record_id": "piece-b1",
                "tenant_id": "tenant-b",
                "employee_id": "employee-b1",
                "dept_id": "group-b1",
                "order_id": "order-b1",
                "style_id": "style-b1",
                "operation_id": "operation-b1",
                "plan_id": None,
                "work_at": "2026-08-20T10:00:00Z",
                "completed_quantity": "5",
                "qualified_quantity": "5",
                "defective_quantity": "0",
                "unit_rate": "1.0000",
                "amount": "5.0000",
                "status": "reported",
            },
        ],
        "payroll_settlements": [
            {
                "settlement_id": "settlement-published",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a1",
                "dept_id": "group-a1",
                "period_start": "2026-07-01",
                "period_end": "2026-07-31",
                "piece_count": "4",
                "gross_amount": "5.0000",
                "status": "published",
                "published_at": "2026-08-02T00:00:00Z",
            },
            {
                "settlement_id": "settlement-draft",
                "tenant_id": "tenant-a",
                "employee_id": "employee-a2",
                "dept_id": "group-a2",
                "period_start": "2026-08-01",
                "period_end": "2026-08-31",
                "piece_count": "6",
                "gross_amount": "6.0000",
                "status": "draft",
                "published_at": None,
            },
        ],
    }
    if scenario == "standard":
        # This PRNG produces reproducible synthetic fixtures, not security values.
        generator = random.Random(seed)  # nosec B311
        for index in range(20):
            quantity = generator.randint(1, 8)
            resources["piecework_records"].append(
                {
                    "record_id": f"piece-standard-{index:03d}",
                    "tenant_id": "tenant-a",
                    "employee_id": "employee-a1" if index % 2 == 0 else "employee-a2",
                    "dept_id": "group-a1" if index % 2 == 0 else "group-a2",
                    "order_id": "order-a1",
                    "style_id": "style-a1",
                    "operation_id": "operation-a1",
                    "plan_id": "plan-a1",
                    "work_at": f"2026-08-{(index % 20) + 1:02d}T12:00:00Z",
                    "completed_quantity": str(quantity),
                    "qualified_quantity": str(quantity),
                    "defective_quantity": "0",
                    "unit_rate": "1.2500",
                    "amount": str(Decimal(quantity) * Decimal("1.2500")),
                    "status": "reported",
                }
            )
    return Dataset(scenario, seed, virtual_now, memberships, scopes, resources)


def reset_database(dataset: Dataset, database_url: str) -> None:
    with psycopg.connect(database_url) as connection, connection.cursor() as cursor:
        cursor.execute("DELETE FROM canonical_resource")
        cursor.execute("DELETE FROM identity_membership")
        cursor.execute("DELETE FROM seed_state")
        for subject_id, memberships in dataset.memberships_by_subject.items():
            for item in memberships:
                cursor.execute(
                    "INSERT INTO identity_membership "
                    "(membership_id, subject_id, tenant_id, employee_id, payload) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (
                        item["membership_id"],
                        subject_id,
                        item["tenant_id"],
                        item["employee_id"],
                        Jsonb(item),
                    ),
                )
        for resource_type, records in dataset.resources.items():
            id_field = RESOURCE_ID_FIELDS[resource_type]
            for item in records:
                occurred_at = (
                    item.get("work_at") or item.get("starts_at") or item.get("effective_from")
                )
                cursor.execute(
                    "INSERT INTO canonical_resource "
                    "(resource_type, resource_id, tenant_id, occurred_at, payload) "
                    "VALUES (%s, %s, %s, %s, %s)",
                    (resource_type, item[id_field], item["tenant_id"], occurred_at, Jsonb(item)),
                )
        cursor.execute(
            "INSERT INTO seed_state (scenario, seed, virtual_now, dataset_hash) "
            "VALUES (%s, %s, %s, %s)",
            (dataset.scenario, dataset.seed, dataset.virtual_now, dataset.digest()),
        )


def main(argv: Sequence[str] | None = None) -> None:
    settings = get_settings()
    parser = argparse.ArgumentParser(description="Build or reset a deterministic Mock MES dataset")
    parser.add_argument("--scenario", choices=("small", "standard"), default=settings.scenario)
    parser.add_argument("--seed", type=int, default=settings.seed)
    parser.add_argument("--virtual-now", default=settings.virtual_now.isoformat())
    args = parser.parse_args(argv)
    virtual_now = datetime.fromisoformat(args.virtual_now.replace("Z", "+00:00"))
    dataset = build_dataset(args.scenario, args.seed, virtual_now)
    if settings.database_url is not None:
        reset_database(dataset, settings.database_url.get_secret_value())
    quantity, amount = dataset.piecework_totals("tenant-a")
    print(
        json.dumps(
            {
                "scenario": dataset.scenario,
                "seed": dataset.seed,
                "virtual_now": dataset.virtual_now.isoformat(),
                "dataset_hash": dataset.digest(),
                "tenant_a_piecework_quantity": str(quantity),
                "tenant_a_piecework_amount": str(amount),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
