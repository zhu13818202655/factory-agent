"""Mock MES PostgreSQL data base.

Creates the persistent data base for the customer-shaped mock: master data,
production chain, the three piecework sources, scan progress, and the
generation batch table. Every business table stores the full customer ``Record``
in ``payload`` (JSONB) and mirrors the columns needed for SQL row-level
filtering (``company`` / ``dept`` / ``uid``) and SQL aggregation (``sl``,
``je``, ``fhsl``, ``baohao``) so the API never loads whole tables into memory.

Schema changes arrive only through Alembic; startup code never creates tables.

Revision ID: 20260829_0001_mock_mes
Revises:
Create Date: 2026-08-29
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260829_0001_mock_mes"
down_revision: str | None = None
branch_labels: str | tuple[str, ...] | None = None
depends_on: str | tuple[str, ...] | None = None


def _create_record_table(
    name: str,
    *,
    id_type: sa.types.TypeEngine[object] = sa.Text,
    extra: list[sa.Column[object]] | None = None,
) -> None:
    """Standard business table: id, tenant, day, JSONB payload + extra columns."""
    columns: list[sa.Column[object]] = [
        sa.Column("id", id_type, primary_key=True),
        sa.Column("company", sa.Text, nullable=False),
        sa.Column("day", sa.Date, nullable=True),
        sa.Column("payload", postgresql.JSONB, nullable=False),
    ]
    if extra:
        columns.extend(extra)
    op.create_table(name, *columns)


def upgrade() -> None:
    # ---- Master data ------------------------------------------------------
    _create_record_table(
        "mock_dept",
        extra=[sa.Column("dept", sa.Text), sa.Column("name", sa.Text)],
    )
    _create_record_table(
        "mock_employee",
        extra=[sa.Column("uid", sa.Text), sa.Column("dept", sa.Text), sa.Column("uname", sa.Text)],
    )
    _create_record_table(
        "mock_huohao",
        extra=[sa.Column("bh", sa.Text), sa.Column("huohaoname", sa.Text)],
    )
    _create_record_table(
        "mock_sc_type", extra=[sa.Column("bh", sa.Text), sa.Column("name", sa.Text)]
    )
    _create_record_table(
        "mock_rfid_worktype",
        extra=[
            sa.Column("bh", sa.Text),
            sa.Column("name", sa.Text),
            sa.Column("wt_sort", sa.Integer),
        ],
    )
    _create_record_table(
        "mock_huohao_worktype",
        extra=[sa.Column("huohao", sa.Text), sa.Column("wt", sa.Text), sa.Column("dept", sa.Text)],
    )
    _create_record_table(
        "mock_user_info",
        extra=[sa.Column("code", sa.Text), sa.Column("username", sa.Text)],
    )
    _create_record_table(
        "mock_move_menu",
        extra=[sa.Column("uid", sa.Text), sa.Column("dept", sa.Text), sa.Column("uname", sa.Text)],
    )
    _create_record_table(
        "mock_dg",
        extra=[sa.Column("dg_name", sa.Text)],
    )
    _create_record_table(
        "mock_dg_zu",
        extra=[sa.Column("dgname", sa.Text)],
    )

    # ---- Production chain -------------------------------------------------
    _create_record_table(
        "mock_plan",
        extra=[sa.Column("dh", sa.Text), sa.Column("dept", sa.Text), sa.Column("zhdate", sa.Date)],
    )
    _create_record_table(
        "mock_sclzd",
        extra=[sa.Column("dh", sa.Text), sa.Column("dept", sa.Text), sa.Column("zhdate", sa.Date)],
    )
    _create_record_table(
        "mock_sclzd_worktype",
        extra=[sa.Column("dh", sa.Text), sa.Column("wt", sa.Text), sa.Column("dept", sa.Text)],
    )

    # ---- Piecework sources (scan / hanging / manual) ----------------------
    _create_record_table(
        "mock_barcode",
        extra=[
            sa.Column("dh", sa.Text),
            sa.Column("detail_id", sa.Text),
            sa.Column("worktype", sa.Text),
            sa.Column("uid", sa.Text),
            sa.Column("dept", sa.Text),
            sa.Column("inputtime", sa.Text),
        ],
    )
    _create_record_table(
        "mock_barcode_cl",
        extra=[
            sa.Column("rq", sa.Date),
            sa.Column("uid", sa.Text),
            sa.Column("worktype", sa.Text),
            sa.Column("huohao", sa.Text),
            sa.Column("dept", sa.Text),
            sa.Column("sl", sa.Numeric(14, 4)),
            sa.Column("je", sa.Numeric(14, 4)),
            sa.Column("fhsl", sa.Numeric(14, 4)),
            sa.Column("baohao", sa.Text),
        ],
    )
    _create_record_table(
        "mock_dg_cl",
        extra=[
            sa.Column("rq", sa.Date),
            sa.Column("uid", sa.Text),
            sa.Column("worktype", sa.Text),
            sa.Column("huohao", sa.Text),
            sa.Column("dept", sa.Text),
            sa.Column("sl", sa.Numeric(14, 4)),
            sa.Column("je", sa.Numeric(14, 4)),
            sa.Column("fhsl", sa.Numeric(14, 4)),
            sa.Column("baohao", sa.Text),
        ],
    )
    _create_record_table(
        "mock_pin_feng",
        extra=[
            sa.Column("zhdate", sa.Date),
            sa.Column("uid", sa.Text),
            sa.Column("worktype", sa.Text),
            sa.Column("huohao", sa.Text),
            sa.Column("dept", sa.Text),
            sa.Column("sl", sa.Numeric(14, 4)),
            sa.Column("je", sa.Numeric(14, 4)),
            sa.Column("fhsl", sa.Numeric(14, 4)),
            sa.Column("baohao", sa.Text),
        ],
    )
    _create_record_table(
        "mock_ysk",
        extra=[
            sa.Column("rq", sa.Date),
            sa.Column("uid", sa.Text),
            sa.Column("worktype", sa.Text),
            sa.Column("huohao", sa.Text),
            sa.Column("dept", sa.Text),
            sa.Column("sl", sa.Numeric(14, 4)),
            sa.Column("je", sa.Numeric(14, 4)),
            sa.Column("fhsl", sa.Numeric(14, 4)),
            sa.Column("baohao", sa.Text),
        ],
    )
    _create_record_table(
        "mock_wsk",
        extra=[
            sa.Column("worktype", sa.Text),
            sa.Column("huohao", sa.Text),
            sa.Column("dept", sa.Text),
            sa.Column("sl", sa.Numeric(14, 4)),
            sa.Column("baohao", sa.Text),
        ],
    )

    # ---- Generation batch ledger -------------------------------------------
    op.create_table(
        "mock_generate_batch",
        sa.Column("day", sa.Date, nullable=False),
        sa.Column("scenario", sa.Text, nullable=False),
        sa.Column("seed", sa.BigInteger, nullable=False),
        sa.Column("run_id", sa.Text, nullable=False),
        sa.Column("row_count", sa.Integer, nullable=False),
        sa.Column("data_hash", sa.Text, nullable=False),
        sa.Column("status", sa.Text, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("day", "scenario", "seed"),
    )

    # ---- Indexes for SQL filtering / windows -------------------------------
    op.create_index("mock_dept_company_idx", "mock_dept", ["company"])
    op.create_index("mock_employee_company_dept_idx", "mock_employee", ["company", "dept"])
    op.create_index("mock_huohao_company_idx", "mock_huohao", ["company"])
    op.create_index("mock_sc_type_company_idx", "mock_sc_type", ["company"])
    op.create_index("mock_rfid_worktype_company_idx", "mock_rfid_worktype", ["company"])
    op.create_index(
        "mock_huohao_worktype_company_huohao_idx", "mock_huohao_worktype", ["company", "huohao"]
    )
    op.create_index("mock_user_info_company_idx", "mock_user_info", ["company"])
    op.create_index("mock_move_menu_company_dept_idx", "mock_move_menu", ["company", "dept"])
    op.create_index("mock_dg_company_idx", "mock_dg", ["company"])
    op.create_index("mock_dg_zu_company_idx", "mock_dg_zu", ["company"])

    op.create_index("mock_plan_company_dept_zhdate_idx", "mock_plan", ["company", "dept", "zhdate"])
    op.create_index(
        "mock_sclzd_company_dept_zhdate_idx", "mock_sclzd", ["company", "dept", "zhdate"]
    )
    op.create_index("mock_sclzd_worktype_company_dh_idx", "mock_sclzd_worktype", ["company", "dh"])

    op.create_index(
        "mock_barcode_company_dh_detail_idx", "mock_barcode", ["company", "dh", "detail_id"]
    )
    op.create_index(
        "mock_barcode_cl_company_dept_rq_idx", "mock_barcode_cl", ["company", "dept", "rq"]
    )
    op.create_index("mock_barcode_cl_uid_idx", "mock_barcode_cl", ["uid"])
    op.create_index("mock_dg_cl_company_dept_rq_idx", "mock_dg_cl", ["company", "dept", "rq"])
    op.create_index("mock_pin_feng_company_dept_idx", "mock_pin_feng", ["company", "dept"])
    op.create_index("mock_ysk_company_dept_rq_idx", "mock_ysk", ["company", "dept", "rq"])
    # Factory-scale windows: the date-range lookup without a dept filter is the
    # hottest path (boss view, footer aggregation), so it gets its own index.
    op.create_index("mock_barcode_cl_company_rq_idx", "mock_barcode_cl", ["company", "rq"])
    op.create_index(
        "mock_barcode_cl_company_dept_uid_rq_idx",
        "mock_barcode_cl",
        ["company", "dept", "uid", "rq"],
    )
    op.create_index("mock_ysk_company_rq_idx", "mock_ysk", ["company", "rq"])
    op.create_index(
        "mock_ysk_company_dept_uid_rq_idx", "mock_ysk", ["company", "dept", "uid", "rq"]
    )
    op.create_index("mock_dg_cl_company_rq_idx", "mock_dg_cl", ["company", "rq"])
    op.create_index("mock_pin_feng_company_zhdate_idx", "mock_pin_feng", ["company", "zhdate"])
    op.create_index("mock_wsk_company_dept_idx", "mock_wsk", ["company", "dept"])

    op.create_index("mock_generate_batch_day_idx", "mock_generate_batch", ["day"])


def downgrade() -> None:
    for table in (
        "mock_generate_batch",
        "mock_wsk",
        "mock_ysk",
        "mock_pin_feng",
        "mock_dg_cl",
        "mock_barcode_cl",
        "mock_barcode",
        "mock_sclzd_worktype",
        "mock_sclzd",
        "mock_plan",
        "mock_dg_zu",
        "mock_dg",
        "mock_move_menu",
        "mock_user_info",
        "mock_huohao_worktype",
        "mock_rfid_worktype",
        "mock_sc_type",
        "mock_huohao",
        "mock_employee",
        "mock_dept",
    ):
        op.drop_table(table)
