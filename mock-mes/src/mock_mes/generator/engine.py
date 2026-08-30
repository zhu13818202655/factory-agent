"""Production-like daily data generator over PostgreSQL (Story 10).

The generator replaces the removed in-memory ``build_dataset``: every day of
the data window is computed deterministically from the factory-scale settings
(``headcount``, ``departments``, ``group_size``, ``daily_active_ratio``, …) and
written to the ``mock_*`` tables by a dedicated writer process. Anchored
Story-5/6/7 fixtures stay byte-identical; rolling rows are generated at real
factory magnitude (work calendar, shifts, delayed orders, defects,
cross-workshop, one-worker-many-orders, scanned/unscanned mix).

``compute_day_rows`` is a pure function of ``(settings, day, prior_ssl)`` so
determinism, invariants and window-boundary behaviour are unit-testable
without a database; ``generate_day`` persists it with batch COPY and records a
per-day batch row (``mock_generate_batch``) for replay/hash auditing.
"""

from __future__ import annotations

import hashlib
import json
import random
import uuid
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from functools import lru_cache
from typing import Any, Sequence

from mock_mes.config import MockMesSettings
from mock_mes.generator.fixtures import (
    HOLIDAYS,
    ROLE_GROUP_LEADER,
    ROLE_WORKER,
    DayPlan,
    RowInsert,
    anchored_rows,
    master_rows,
)

#: Table -> mirrored filter/aggregate columns extracted from the payload.
_COLUMN_MAP: dict[str, tuple[str, ...]] = {
    "mock_dept": ("dept", "name"),
    "mock_employee": ("uid", "dept", "uname"),
    "mock_huohao": ("bh", "huohaoname"),
    "mock_sc_type": ("bh", "name"),
    "mock_rfid_worktype": ("bh", "name", "wt_sort"),
    "mock_huohao_worktype": ("huohao", "wt", "dept"),
    "mock_user_info": ("code", "username"),
    "mock_move_menu": ("uid", "dept", "uname"),
    "mock_dg": ("dg_name",),
    "mock_dg_zu": ("dgname",),
    "mock_plan": ("dh", "dept", "zhdate"),
    "mock_sclzd": ("dh", "dept", "zhdate"),
    "mock_sclzd_worktype": ("dh", "wt", "dept"),
    "mock_barcode": ("dh", "detail_id", "worktype", "uid", "dept", "inputtime"),
    "mock_barcode_cl": ("rq", "uid", "worktype", "huohao", "dept", "sl", "je", "fhsl", "baohao"),
    "mock_dg_cl": ("rq", "uid", "worktype", "huohao", "dept", "sl", "je", "fhsl", "baohao"),
    "mock_pin_feng": ("zhdate", "uid", "worktype", "huohao", "dept", "sl", "je", "fhsl", "baohao"),
    "mock_ysk": ("rq", "uid", "worktype", "huohao", "dept", "sl", "je", "fhsl", "baohao"),
    "mock_wsk": ("worktype", "huohao", "dept", "sl", "baohao"),
}

#: Price per worktype (确定性计件单价, Story 6 M9/M18).
_PRICE: dict[str, str] = {"WT01": "1.2500", "WT02": "0.8000", "WT03": "1.0000"}
_WTNAME: dict[str, str] = {"WT01": "平车", "WT02": "手工钉扣", "WT03": "吊挂平车"}
_CHIMA = ("M", "L", "S", "XL", "XXL")

#: Master tables are upserted on every day and are not part of a day's batch.
_MASTER_TABLES = frozenset(
    {
        "mock_dept",
        "mock_employee",
        "mock_huohao",
        "mock_sc_type",
        "mock_rfid_worktype",
        "mock_huohao_worktype",
        "mock_user_info",
        "mock_move_menu",
        "mock_dg",
        "mock_dg_zu",
    }
)


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def is_workday(day: date) -> bool:
    """Production day: Monday-Friday and not a listed holiday."""
    if day.weekday() >= 5:
        return False
    return day not in HOLIDAYS


def _rng(seed: int, day: date, salt: str) -> random.Random:
    return random.Random(f"{seed}:{day.isoformat()}:{salt}")  # nosec B311 - deterministic fixtures


def day_digest(inserts: Sequence[RowInsert]) -> str:
    """Deterministic batch hash: JSON-normalised hash of every written payload."""
    merged: list[dict[str, object]] = []
    for row in inserts:
        merged.append({"_t": row.table, **row.payload})
    merged.sort(key=lambda item: str(item.get("_t")) + ":" + str(item.get("id", "")))
    encoded = json.dumps(merged, sort_keys=True, separators=(",", ":"), default=str).encode()
    return hashlib.sha256(encoded).hexdigest()


@lru_cache(maxsize=8)
def _worker_pool(
    headcount: int,
    departments: int,
    group_size: int,
    headcount_secondary: int,
    styles: int,
) -> tuple[tuple[str, str, str, str, str], ...]:
    """People who can record piecework output, cached per scale parameters.

    Managers (02) and the boss (99) have no personal output records; workers
    (00) and group leaders (01) do — a group leader is still a pieceworker in
    a real garment plant.
    """
    settings = MockMesSettings(
        headcount=headcount,
        departments=departments,
        group_size=group_size,
        headcount_secondary=headcount_secondary,
        styles=styles,
    )
    rows = master_rows(settings)
    pool: list[tuple[str, str, str, str, str]] = []
    for row in rows:
        if row.table != "mock_employee":
            continue
        payload = row.payload
        role = str(payload["move_admin_role"])
        if role not in (ROLE_WORKER, ROLE_GROUP_LEADER):
            continue
        pool.append(
            (
                str(payload["uid"]),
                str(payload["uname"]),
                str(payload["dept"]),
                str(payload["deptname"]),
                str(payload["company"]),
            )
        )
    return tuple(pool)


@lru_cache(maxsize=8)
def _style_pool(styles: int) -> tuple[tuple[str, str, str, str], ...]:
    """(bh, breed, description, colour) catalogue for the given style count."""
    settings = MockMesSettings(styles=styles)
    rows = master_rows(settings)
    pool: list[tuple[str, str, str, str]] = []
    colors = ("黑色", "白色", "蓝色", "红色", "灰色", "米色", "藏青", "卡其")
    for index, row in enumerate(r for r in rows if r.table == "mock_huohao"):
        payload = row.payload
        pool.append(
            (
                str(payload["bh"]),
                str(payload["bbreed"]),
                str(payload["description"]),
                colors[index % len(colors)],
            )
        )
    return tuple(pool)


# ---------------------------------------------------------------------------
# Rolling rows (real-scale daily output).
# ---------------------------------------------------------------------------


def rolling_rows(settings: MockMesSettings, day: date, prior_ssl: dict[str, Decimal]) -> DayPlan:
    """Deterministic rolling business rows for one day at factory scale."""
    plan = DayPlan(day=day)
    if not is_workday(day):
        return plan

    rng = _rng(settings.seed, day, "rolling")
    workers = _worker_pool(
        settings.headcount,
        settings.departments,
        settings.group_size,
        settings.headcount_secondary,
        settings.styles,
    )
    styles = _style_pool(settings.styles)
    if not workers:
        return plan

    # --- Production plans + orders for the day -----------------------------
    plan_order_ids: list[str] = []
    for index in range(max(settings.plans_per_day, 0)):
        detail_id = f"p{day:%Y%m%d}{index:02d}"
        plan_order_ids.append(detail_id)
        _rolling_plan(plan, settings, day, rng, styles, index, detail_id)

    # --- Piecework output: active workers x scans per worker ---------------
    active_count = int(len(workers) * min(max(settings.daily_active_ratio, 0.0), 1.0))
    active = rng.sample(workers, active_count)
    scanned_totals: dict[str, Decimal] = {}

    for worker_index, (uid, uname, dept, deptname, company) in enumerate(active):
        detail_id = plan_order_ids[worker_index % len(plan_order_ids)]
        for scan_index in range(max(settings.scans_per_worker, 0)):
            style, breed, description, color = styles[(worker_index + scan_index) % len(styles)]
            wt = rng.choice(("WT01", "WT02", "WT03"))
            quantity = Decimal(str(rng.randint(1, 12)))
            price = _PRICE[wt]
            je = quantity * _d(price)
            hour = rng.choice((8, 9, 10, 11, 13, 14, 15, 16, 17))
            minute = rng.choice((0, 10, 20, 30, 40, 50))
            inputtime = f"{day:%Y-%m-%d} {hour:02d}:{minute:02d}:00"
            scan_id = f"c{day:%Y%m%d}{worker_index:03d}{scan_index}"
            fhsl = str(rng.randint(40, 400))
            base: dict[str, object] = {
                "inputtime": inputtime,
                "uid": uid,
                "uname": uname,
                "dept": dept,
                "deptname": deptname,
                "rq": f"{day:%Y-%m-%d}",
                "chuanghao": "床号1",
                "sctype": "SC1",
                "sctypename": "大身",
                "baohao": "包1",
                "id": scan_id,
                "huohao": style,
                "bbreed": breed,
                "description": description,
                "color": color,
                "chima": rng.choice(_CHIMA),
                "worktype": wt,
                "wtname": _WTNAME[wt],
                "fhsl": fhsl,
                "price": price,
                "company": company,
            }
            plan.inserts.append(
                RowInsert(
                    "mock_barcode_cl",
                    payload={**base, "sssl": str(quantity), "sl": fhsl, "je": str(je)},
                    company=company,
                )
            )
            plan.inserts.append(
                RowInsert(
                    "mock_ysk",
                    payload={
                        **base,
                        "inputtime_raw": f"{inputtime}.000",
                        "sl": str(quantity),
                        "je": str(je),
                        "cid": f"cid-{scan_id}",
                        "sffb": 0,
                        "fbid": "",
                    },
                    company=company,
                )
            )
            plan.inserts.append(
                RowInsert(
                    "mock_barcode",
                    payload={
                        "dh": f"ZD-{day:%y%m%d}{worker_index % len(plan_order_ids):02d}",
                        "detailId": detail_id,
                        "uid": uid,
                        "uname": uname,
                        "dept": dept,
                        "worktype": wt,
                        "inputtime": inputtime,
                        "company": company,
                    },
                    company=company,
                )
            )
            if wt == "WT03":
                plan.inserts.append(
                    RowInsert(
                        "mock_dg_cl",
                        payload={
                            "id": f"dgc-{scan_id}",
                            "rq": f"{day:%Y-%m-%d}",
                            "dddh": f"JH-{day:%y%m%d}",
                            "chuanghao": "床号1",
                            "huohao": style,
                            "bbreed": breed,
                            "color": color,
                            "chima": base["chima"],
                            "worktype": wt,
                            "wtname": "吊挂平车",
                            "uid": uid,
                            "uname": uname,
                            "dguid": f"D{uid}",
                            "dguname": f"吊挂{uname}",
                            "dept": dept,
                            "dgName": "一号吊挂线",
                            "dgStyleNo": "1",
                            "sl": str(quantity),
                            "price": price,
                            "je": str(je),
                            "sfjz": 0,
                            "company": company,
                        },
                        company=company,
                    )
                )
            scanned_totals[detail_id] = scanned_totals.get(detail_id, Decimal("0")) + quantity

    # --- Manual entries (手工账) with possible defects ---------------------
    manual_count = max(int(active_count * 0.05), 1) if active_count else 0
    for index in range(manual_count):
        uid, uname, dept, deptname, company = active[(index * 7) % len(active)]
        style, breed, description, color = styles[index % len(styles)]
        quantity = Decimal(str(rng.randint(1, 4)))
        price = _PRICE["WT02"]
        plan.inserts.append(
            RowInsert(
                "mock_pin_feng",
                payload={
                    "dh": f"PF-{day:%y%m%d}-{index:02d}",
                    "zhdate": f"{day:%Y-%m-%d}",
                    "state": 1,
                    "zhuser": "admin",
                    "zhuser_sh": "admin",
                    "id": f"pf-{day:%Y%m%d}-{index:02d}",
                    "dept": dept,
                    "deptname": deptname,
                    "uid": uid,
                    "uname": uname,
                    "huohao": style,
                    "huohaoname": breed,
                    "ddh": f"JH-{day:%y%m%d}",
                    "worktype": "WT02",
                    "wtname": "手工钉扣",
                    "dw": "件",
                    "js": str(quantity),
                    "sl": str(quantity),
                    "cp": str(rng.randint(0, 1)),
                    "chuanghao": "床号1",
                    "color": color,
                    "chima": rng.choice(_CHIMA),
                    "price": price,
                    "je": str(quantity * _d(price)),
                    "remark": "",
                    "company": company,
                },
                company=company,
            )
        )

    # --- Cross-day progress: roll today's scans into sclzd sssl ------------
    for detail_id, today_total in scanned_totals.items():
        if today_total <= 0:
            continue
        plan.ssl_updates.append((detail_id, prior_ssl.get(detail_id, Decimal("0")) + today_total))
    return plan


def _rolling_plan(
    plan: DayPlan,
    settings: MockMesSettings,
    day: date,
    rng: random.Random,
    styles: Sequence[tuple[str, str, str, str]],
    index: int,
    detail_id: str,
) -> None:
    """One rolling plan + production order + worktypes (production chain)."""
    company = "COMPANY-A"
    style, breed, description, color = styles[index % len(styles)]
    chima = rng.choice(_CHIMA)
    dh = f"PLAN-{day:%y%m%d}{index:02d}"
    total = rng.randint(80, 600)
    # ~15% delayed order: finish date already in the past.
    finish = day + timedelta(days=rng.randint(10, 45))
    if rng.random() < 0.15:
        finish = day - timedelta(days=rng.randint(1, 7))
    plan.inserts.append(
        RowInsert(
            "mock_plan",
            payload={
                "dh": dh,
                "zhdate": f"{day:%Y-%m-%d}",
                "finish_date": f"{finish:%Y-%m-%d}",
                "jhdh": f"JH-{day:%y%m%d}{index:02d}",
                "hth": f"HT-{day:%y%m%d}{index:02d}",
                "gdy": "模拟跟单员",
                "zdr": "admin",
                "zsl": str(total),
                "zdr_sh": "admin",
                "state": 1,
                "id": f"plan-guid-{day:%Y%m%d}{index:02d}",
                "khddh": f"KHDD-{day:%y%m%d}{index:02d}",
                "pinpai": "P1",
                "pinpainame": "模拟品牌",
                "khid": "K001",
                "khname": "模拟客户",
                "khhh": f"KHHH-{day:%y%m%d}{index:02d}",
                "huohao": style,
                "huohaoname": breed,
                "spname": description,
                "color": color,
                "chima": chima,
                "dw": "件",
                "ddsl": str(total),
                "paol": str(rng.randint(0, 3)),
                "sl": str(total),
                "remark": "",
                "company": company,
                "dept": "dept-a1",
            },
            company=company,
        )
    )
    plan.inserts.append(
        RowInsert(
            "mock_sclzd",
            payload={
                "dh": f"ZD-{day:%y%m%d}{index:02d}",
                "zhdate": f"{day:%Y-%m-%d}",
                "dddh": f"JH-{day:%y%m%d}{index:02d}",
                "khid": "K001",
                "khname": "模拟客户",
                "drdg_status": 0,
                "huohao": style,
                "huohaoname": breed,
                "description": description,
                "sctype": "SC1",
                "sctypename": "大身",
                "chuanghao": "床号1",
                "cjr": "模拟裁剪员",
                "zdr": "admin",
                "state": 1,
                "id": detail_id,
                "baohao": "包1",
                "ganghao": "缸1",
                "color": color,
                "chima": chima,
                "fhsl": str(total),
                "sssl": "0",
                "remark": "",
                "company": company,
                "dept": "dept-a1",
            },
            company=company,
        )
    )
    for sort, (wt, wt_name) in enumerate(
        [("WT01", "平车"), ("WT02", "手工钉扣"), ("WT03", "吊挂平车")], start=1
    ):
        plan.inserts.append(
            RowInsert(
                "mock_sclzd_worktype",
                payload={
                    "id": f"sw-{detail_id}-{wt}",
                    "dh": f"ZD-{day:%y%m%d}{index:02d}",
                    "huohao": style,
                    "huohaoname": breed,
                    "wt": wt,
                    "wtname": wt_name,
                    "sort": sort,
                    "zhgx": 1 if sort == 3 else 0,
                    "sfzb": 0,
                    "sctype": "SC1",
                    "sctypename": "大身",
                    "company": company,
                    "dept": "dept-a1",
                },
                company=company,
            )
        )
        plan.inserts.append(
            RowInsert(
                "mock_wsk",
                payload={
                    "id": f"ws-{detail_id}-{wt}",
                    "chuanghao": "床号1",
                    "huohao": style,
                    "color": color,
                    "chima": chima,
                    "worktype": wt,
                    "sl": str(rng.randint(0, 60)),
                    "baohao": "包1",
                    "company": company,
                    "dept": "dept-a1",
                },
                company=company,
            )
        )


# ---------------------------------------------------------------------------
# Day composition + persistence.
# ---------------------------------------------------------------------------


def compute_day_rows(
    settings: MockMesSettings,
    day: date,
    prior_ssl: dict[str, Decimal] | None = None,
) -> DayPlan:
    """Anchored + rolling rows for ``day`` (pure, deterministic)."""
    prior = prior_ssl or {}
    plan = DayPlan(day=day)
    plan.inserts.extend(anchored_rows(day))
    rolling = rolling_rows(settings, day, prior)
    plan.inserts.extend(rolling.inserts)
    plan.ssl_updates.extend(rolling.ssl_updates)

    # Plan-day sclzd rows start at today's scanned quantity (cross-day ssl).
    today_by_detail = {
        detail: total - prior.get(detail, Decimal("0")) for detail, total in plan.ssl_updates
    }
    for index, row in enumerate(plan.inserts):
        if row.table != "mock_sclzd":
            continue
        detail = str(row.payload.get("id"))
        today_qty = today_by_detail.get(detail, Decimal("0"))
        if today_qty <= 0:
            continue
        updated = dict(row.payload)
        updated["sssl"] = str(today_qty)
        plan.inserts[index] = _replace(row, updated)
    return plan


def _replace(row: RowInsert, payload: dict[str, object]) -> RowInsert:
    return RowInsert(table=row.table, payload=payload, id=row.id, company=row.company)


#: Payload key -> mirrored column aliases (Story-5 field names differ from
#: the SQL column names).
_PAYLOAD_KEY_ALIAS: dict[tuple[str, str], str] = {
    ("mock_barcode", "detail_id"): "detailId",
    ("mock_barcode_cl", "sl"): "sssl",  # scanned qty lives in sssl, not sl (fhsl)
}


def _extract_columns(row: RowInsert) -> dict[str, object]:
    """Mirror filter/aggregate columns from the payload for the SQL row."""
    columns: dict[str, object] = {}
    for column in _COLUMN_MAP.get(row.table, ()):
        payload_key = _PAYLOAD_KEY_ALIAS.get((row.table, column), column)
        if payload_key in row.payload:
            columns[column] = row.payload[payload_key]
    return columns


# ---------------------------------------------------------------------------
# SQL persistence (writer process) — batch COPY for factory-scale volumes.
# ---------------------------------------------------------------------------


def _row_id(row: RowInsert) -> str:
    """Deterministic primary key for a row.

    Story-5 records carry a payload ``id`` that is not unique per row (the
    anchored scan/ysk rows reuse the detail id ``1001`` across days, and the
    customer barcode records have no id at all), so the table primary key is
    derived from the payload while the API keeps returning the original shape.
    """
    if row.id is not None:
        return row.id
    table = row.table
    payload = row.payload
    if table == "mock_barcode":
        return f"b-{payload.get('detailId')}-{payload.get('worktype')}-{payload.get('inputtime')}"
    if table in ("mock_barcode_cl", "mock_ysk"):
        return f"{table.split('_')[1]}-{payload.get('id')}-{payload.get('inputtime')}"
    return str(payload.get("id", ""))


def _row_tuple(row: RowInsert, day: date, columns: Sequence[str]) -> tuple[object, ...]:
    extracted = _extract_columns(row)
    company = (
        row.company if row.company is not None else str(row.payload.get("company", "COMPANY-A"))
    )
    return (
        _row_id(row),
        company,
        day,
        json.dumps(row.payload, ensure_ascii=False),
        *(extracted.get(column) for column in columns),
    )


async def _bulk_insert(connection: Any, table: str, rows: Sequence[RowInsert], day: date) -> None:
    """COPY into a temp table, then INSERT ... ON CONFLICT DO NOTHING.

    Row-at-a-time INSERT is far too slow at factory scale (millions of rows);
    COPY plus a set-based insert keeps generation in minutes while staying
    idempotent.
    """
    if not rows:
        return
    columns = tuple(_COLUMN_MAP.get(table, ()))
    column_list = ["id", "company", "day", "payload", *columns]
    joined = ", ".join(column_list)
    staging = f"tmp_{table}_{uuid.uuid4().hex[:8]}"
    async with connection.cursor() as cursor:
        await cursor.execute(f"CREATE TEMP TABLE {staging} (LIKE {table})")  # nosec B608 - fixed table names
        async with cursor.copy(f"COPY {staging} ({joined}) FROM STDIN") as copy:  # nosec B608
            for row in rows:
                await copy.write_row(_row_tuple(row, day, columns))
        await cursor.execute(  # nosec B608 - fixed table/column names
            f"INSERT INTO {table} ({joined}) SELECT {joined} FROM {staging} "
            f"ON CONFLICT (id) DO NOTHING"
        )
        await cursor.execute(f"DROP TABLE {staging}")  # nosec B608


def _group_by_table(rows: Sequence[RowInsert]) -> dict[str, list[RowInsert]]:
    grouped: dict[str, list[RowInsert]] = {}
    for row in rows:
        grouped.setdefault(row.table, []).append(row)
    return grouped


async def _upsert_master(connection: Any, settings: MockMesSettings, day: date) -> None:
    for table, rows in _group_by_table(master_rows(settings)).items():
        await _bulk_insert(connection, table, rows, day)


async def _apply_ssl_updates(connection: Any, updates: Sequence[tuple[str, Decimal]]) -> None:
    """Roll today's scans into the order progress, capped at the plan quantity.

    A real production order never completes more pieces than it was planned
    for, so the cumulative scanned quantity is clamped to ``fhsl``.
    """
    for detail_id, total in updates:
        await connection.execute(
            "UPDATE mock_sclzd SET payload = jsonb_set(payload, '{sssl}', to_jsonb("
            "LEAST(%(total)s::numeric, NULLIF(payload->>'fhsl', '')::numeric)::text)) "
            "WHERE id = %(id)s",
            {"id": detail_id, "total": str(total)},
        )


@dataclass(frozen=True, slots=True)
class DayBatch:
    day: date
    seed: int
    status: str  # "generated" | "skipped"
    row_count: int
    data_hash: str


async def _batch_exists(connection: Any, seed: int, day: date) -> bool:
    cursor = await connection.execute(
        "SELECT 1 FROM mock_generate_batch WHERE day = %(day)s AND seed = %(seed)s",
        {"day": day, "seed": seed},
    )
    return await cursor.fetchone() is not None


async def _record_batch(
    connection: Any,
    seed: int,
    day: date,
    run_id: str,
    row_count: int,
    data_hash: str,
) -> None:
    await connection.execute(
        "INSERT INTO mock_generate_batch (day, scenario, seed, run_id, row_count, data_hash, "
        "status, created_at) VALUES (%(day)s, 'default', %(seed)s, %(run_id)s, %(row_count)s, "
        "%(data_hash)s, 'done', NOW()) ON CONFLICT (day, scenario, seed) DO NOTHING",
        {
            "day": day,
            "seed": seed,
            "run_id": run_id,
            "row_count": row_count,
            "data_hash": data_hash,
        },
    )


async def generate_day(
    connection: Any,
    settings: MockMesSettings,
    day: date,
    run_id: str,
) -> DayBatch:
    """Generate one day idempotently; already-generated days are skipped."""
    if await _batch_exists(connection, settings.seed, day):
        return DayBatch(day=day, seed=settings.seed, status="skipped", row_count=0, data_hash="")

    # Prior cumulative ssl for the rolling orders created on this day.
    prior_ssl: dict[str, Decimal] = {}
    cursor = await connection.execute(
        "SELECT id, payload->>'sssl' AS sssl FROM mock_sclzd WHERE id LIKE %(prefix)s",
        {"prefix": f"p{day:%Y%m%d}%"},
    )
    for row in await cursor.fetchall():
        sssl = row["sssl"]
        prior_ssl[str(row["id"])] = Decimal(sssl) if sssl not in (None, "") else Decimal("0")

    plan = compute_day_rows(settings, day, prior_ssl)
    await _upsert_master(connection, settings, day)
    for table, rows in _group_by_table(plan.inserts).items():
        await _bulk_insert(connection, table, rows, day)
    await _apply_ssl_updates(connection, plan.ssl_updates)

    business_rows = [row for row in plan.inserts if row.table not in _MASTER_TABLES]
    data_hash = day_digest(plan.inserts)
    await _record_batch(connection, settings.seed, day, run_id, len(business_rows), data_hash)
    return DayBatch(
        day=day,
        seed=settings.seed,
        status="generated",
        row_count=len(business_rows),
        data_hash=data_hash,
    )


@dataclass(frozen=True, slots=True)
class FillReport:
    window_start: date
    window_end: date
    generated: int
    skipped: int
    batches: list[DayBatch]


async def fill_window(
    connection: Any,
    settings: MockMesSettings,
    start: date,
    end: date,
    run_id: str,
) -> FillReport:
    """Scan the window in order and fill only missing days (缺日补齐)."""
    generated: list[DayBatch] = []
    skipped = 0
    cursor = start
    while cursor <= end:
        batch = await generate_day(connection, settings, cursor, run_id)
        if batch.status == "generated":
            generated.append(batch)
            # Commit per day so a long backfill is resumable.
            await connection.commit()
        else:
            skipped += 1
        cursor += timedelta(days=1)
    return FillReport(
        window_start=start,
        window_end=end,
        generated=len(generated),
        skipped=skipped,
        batches=generated,
    )


async def window_digest(
    connection: Any,
    settings: MockMesSettings,
    start: date,
    end: date,
) -> str:
    """Deterministic whole-window hash from the batch ledger (对拍基线)."""
    cursor = await connection.execute(
        "SELECT data_hash FROM mock_generate_batch WHERE day BETWEEN %(start)s AND %(end)s "
        "AND seed = %(seed)s ORDER BY day",
        {"start": start, "end": end, "seed": settings.seed},
    )
    hashes = [str(row["data_hash"]) for row in await cursor.fetchall()]
    if not hashes:
        return ""
    encoded = "\n".join(hashes).encode()
    return hashlib.sha256(encoded).hexdigest()


__all__ = [
    "DayBatch",
    "DayPlan",
    "FillReport",
    "compute_day_rows",
    "day_digest",
    "fill_window",
    "generate_day",
    "is_workday",
    "rolling_rows",
    "window_digest",
]
