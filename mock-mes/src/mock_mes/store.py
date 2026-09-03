"""Read-only data access over PostgreSQL.

Every endpoint query maps to SQL over the ``mock_*`` tables: row-level
filtering is pushed into the ``WHERE`` clause (company → role scope),
pagination and footers use SQL COUNT/SUM, and the returned rows keep the exact
customer-shaped ``Record`` shapes so ``api/customer.py`` application logic
barely changes.

Role scope (contract: ``docs/product/需求及方案整理.md``「角色权限与数据校验
策略」): ``99`` boss sees the whole company, ``02`` manager sees every bound
车间/部门 (a manager may bind several workshops), ``01`` group leader sees their
bound 小组 on personal rows (uid-attributed tables) and their department on
organisational tables (mock simplification; real-MES group binding is a joint-
debug item), and ``00`` worker sees own rows only. **Base-data interfaces do not
filter by role** (``role_scoped=False``) — 员工/部门 etc. return the full
company set for any authenticated caller.

The three piecework sources are still merged in Python because the customer
contract normalises them into a single wage row set (Type 0/1/2); the SQL
side always filters by tenant/role/uid/date first so whole tables are never
loaded into memory.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import Iterable, Sequence, cast

from mock_mes.db import MockMesDb
from mock_mes.generator.fixtures import ROLE_BOSS, ROLE_GROUP_LEADER, ROLE_WORKER
from mock_mes.identities import Record

#: (date_column) for windowed tables; others are filtered by day-less columns.
_WINDOWED = {
    "mock_plan": "zhdate",
    "mock_sclzd": "zhdate",
    "mock_barcode_cl": "rq",
    "mock_dg_cl": "rq",
    "mock_pin_feng": "zhdate",
    "mock_ysk": "rq",
}

#: Tables whose payload lacks a tenant column are seeded under
#: COMPANY-A and are not company-filtered by their endpoints.
_TENANT_NEUTRAL = {
    "mock_user_info",
    "mock_huohao",
    "mock_sc_type",
    "mock_rfid_worktype",
    "mock_huohao_worktype",
}

#: Mirror columns actually available per table for row-level filtering. The
#: dept/uid clauses are only appended when the table has that column — tables
#: without a ``uid`` column (orders, workshops, hanging lines) are dept-scoped
#: for workers instead of own-data-scoped, and hanging-line tables are only
#: company-scoped.
_FILTER_COLUMNS: dict[str, frozenset[str]] = {
    "mock_dept": frozenset({"company", "dept"}),
    "mock_employee": frozenset({"company", "dept", "uid"}),
    "mock_huohao": frozenset({"company"}),
    "mock_huohao_worktype": frozenset({"company", "dept"}),
    "mock_move_menu": frozenset({"company", "dept", "uid"}),
    "mock_dg": frozenset({"company"}),
    "mock_dg_zu": frozenset({"company"}),
    "mock_plan": frozenset({"company", "dept"}),
    "mock_sclzd": frozenset({"company", "dept"}),
    "mock_sclzd_worktype": frozenset({"company", "dept"}),
    "mock_barcode": frozenset({"company", "dept", "uid"}),
    "mock_barcode_cl": frozenset({"company", "dept", "uid"}),
    "mock_dg_cl": frozenset({"company", "dept", "uid"}),
    "mock_pin_feng": frozenset({"company", "dept", "uid"}),
    "mock_ysk": frozenset({"company", "dept", "uid"}),
    "mock_wsk": frozenset({"company", "dept"}),
}
_ALL_FILTER_COLUMNS = frozenset({"company", "dept", "uid"})


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def _bound_depts(identity: Record) -> list[str]:
    """Dept codes the caller may see: ``dept`` plus any manager ``boundDepts``.

    02 管理 may bind several 车间/部门 (cross-workshop); every other role
    carries at most its own ``dept``. The primary dept is always first.
    """
    dept = identity.get("dept")
    if not dept:
        return []
    codes: list[str] = [str(dept)]
    bound = identity.get("boundDepts")
    if isinstance(bound, (list, tuple)):
        for code in cast("list[object]", bound):
            text = str(code)
            if text and text not in codes:
                codes.append(text)
    return codes


@dataclass(frozen=True, slots=True)
class Page:
    """SQL-paginated result with total."""

    items: list[Record]
    total: int
    offset: int
    limit: int


class MockMesStore:
    """Read-only facade over the ``mock_*`` tables."""

    def __init__(self, db: MockMesDb) -> None:
        self._db = db

    # ------------------------------------------------------------------
    # Filter / pagination helpers (SQL).
    # ------------------------------------------------------------------

    def _filter_sql(
        self,
        table: str,
        identity: Record,
        *,
        tenant_scoped: bool = True,
        role_scoped: bool = True,
        extra: Sequence[tuple[str, str, object]] = (),
        date_field: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> tuple[str, dict[str, object]]:
        """Build the row-level filtering WHERE plus extra clauses.

        Company isolation always applies (except tenant-neutral tables). Role
        scope applies only when ``role_scoped`` is true — base-data endpoints
        pass ``role_scoped=False`` because 员工/部门等基础数据接口不按权限过滤.

        Extra clauses are ``(column, operator, value)`` triples.
        """
        clauses: list[str] = []
        params: dict[str, object] = {}
        available = _FILTER_COLUMNS.get(table, _ALL_FILTER_COLUMNS)

        if tenant_scoped and table not in _TENANT_NEUTRAL:
            clauses.append("company = %(company)s")
            params["company"] = str(identity["company"])
            if role_scoped:
                role = str(identity.get("move_admin_role", ROLE_WORKER))
                if role != ROLE_BOSS:
                    # Boss (99) is company-wide; every other tier is narrower.
                    if role == ROLE_GROUP_LEADER and identity.get("group") and "uid" in available:
                        # 01 组长 sees their bound 小组: rows attributed to the
                        # group's member uids (employee-master group_id).
                        clauses.append(
                            "(uid IS NULL OR uid IN (SELECT uid FROM mock_employee "
                            "WHERE company = %(company)s AND group_id = %(group)s))"
                        )
                        params["group"] = str(identity["group"])
                    else:
                        # 02 管理 sees every bound 车间/部门 (possibly several,
                        # incl. cross-workshop). 01 组长 on organisational
                        # tables (no uid) and 00 员工 are dept-scoped.
                        bound_depts = _bound_depts(identity)
                        if bound_depts and "dept" in available:
                            clauses.append("(dept IS NULL OR dept = ANY(%(dept_ids)s))")
                            params["dept_ids"] = bound_depts
                    if role == ROLE_WORKER and "uid" in available:
                        # 00 worker sees own rows only on personal tables.
                        clauses.append("(uid IS NULL OR uid = %(uid)s)")
                        params["uid"] = str(identity["user"])

        for column, operator, value in extra:
            clauses.append(f"{column} {operator} %({column})s")
            params[column] = value

        if date_field is not None:
            if start is not None:
                clauses.append(f"{date_field} >= %(start)s")
                params["start"] = start
            if end is not None:
                clauses.append(f"{date_field} <= %(end)s")
                params["end"] = end

        where = " AND ".join(clauses) if clauses else "TRUE"
        return f"WHERE {where}", params  # nosec B608 - fixed column/table names

    async def count(
        self,
        table: str,
        identity: Record,
        *,
        tenant_scoped: bool = True,
        role_scoped: bool = True,
        extra: Sequence[tuple[str, str, object]] = (),
        date_field: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> int:
        where, params = self._filter_sql(
            table,
            identity,
            tenant_scoped=tenant_scoped,
            role_scoped=role_scoped,
            extra=extra,
            date_field=date_field,
            start=start,
            end=end,
        )
        row = await self._db.fetchone(
            f"SELECT COUNT(*) AS n FROM {table} {where}",  # nosec B608 - fixed table names
            params,
        )
        return int(row["n"]) if row else 0

    async def page(
        self,
        table: str,
        identity: Record,
        *,
        tenant_scoped: bool = True,
        role_scoped: bool = True,
        extra: Sequence[tuple[str, str, object]] = (),
        date_field: str | None = None,
        start: date | None = None,
        end: date | None = None,
        page_num: int = 1,
        size: int = 50,
        order_by: str = "id",
    ) -> Page:
        where, params = self._filter_sql(
            table,
            identity,
            tenant_scoped=tenant_scoped,
            role_scoped=role_scoped,
            extra=extra,
            date_field=date_field,
            start=start,
            end=end,
        )
        total = await self.count(
            table,
            identity,
            tenant_scoped=tenant_scoped,
            role_scoped=role_scoped,
            extra=extra,
            date_field=date_field,
            start=start,
            end=end,
        )
        offset = max((page_num - 1) * size, 0)
        page_params = {**params, "offset": offset, "size": size}
        rows = await self._db.fetch(
            f"SELECT payload FROM {table} {where} "  # nosec B608 - fixed table/column names
            f"ORDER BY {order_by} LIMIT %(size)s OFFSET %(offset)s",
            page_params,
        )
        items = [Record(row["payload"]) for row in rows]
        return Page(items=items, total=total, offset=offset, limit=size)

    async def list_rows(
        self,
        table: str,
        identity: Record,
        *,
        tenant_scoped: bool = True,
        role_scoped: bool = True,
        extra: Sequence[tuple[str, str, object]] = (),
        date_field: str | None = None,
        start: date | None = None,
        end: date | None = None,
        order_by: str = "id",
        limit: int | None = None,
    ) -> list[Record]:
        where, params = self._filter_sql(
            table,
            identity,
            tenant_scoped=tenant_scoped,
            role_scoped=role_scoped,
            extra=extra,
            date_field=date_field,
            start=start,
            end=end,
        )
        if limit is not None:
            params = {**params, "limit": limit}
            rows = await self._db.fetch(
                f"SELECT payload FROM {table} {where} ORDER BY {order_by} LIMIT %(limit)s",  # nosec B608
                params,
            )
        else:
            rows = await self._db.fetch(
                f"SELECT payload FROM {table} {where} ORDER BY {order_by}",  # nosec B608 - fixed names
                params,
            )
        return [Record(row["payload"]) for row in rows]

    async def sum_rows(
        self,
        table: str,
        identity: Record,
        fields: Iterable[str],
        *,
        tenant_scoped: bool = True,
        extra: Sequence[tuple[str, str, object]] = (),
        date_field: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> dict[str, str]:
        """SQL SUM of the mirrored numeric columns; returns string totals."""
        field_list = list(fields)
        select = ", ".join(f"COALESCE(SUM({f}), 0) AS {f}" for f in field_list)
        where, params = self._filter_sql(
            table,
            identity,
            tenant_scoped=tenant_scoped,
            extra=extra,
            date_field=date_field,
            start=start,
            end=end,
        )
        row = await self._db.fetchone(
            f"SELECT {select} FROM {table} {where}",  # nosec B608 - fixed table/column names
            params,
        )
        result: dict[str, str] = {}
        for field in field_list:
            value = row[field] if row else None
            result[field] = "0" if value is None else str(value)
        return result

    async def distinct_count(
        self,
        table: str,
        identity: Record,
        column: str,
        *,
        tenant_scoped: bool = True,
        extra: Sequence[tuple[str, str, object]] = (),
        date_field: str | None = None,
        start: date | None = None,
        end: date | None = None,
    ) -> str:
        where, params = self._filter_sql(
            table,
            identity,
            tenant_scoped=tenant_scoped,
            extra=extra,
            date_field=date_field,
            start=start,
            end=end,
        )
        row = await self._db.fetchone(
            f"SELECT COUNT(DISTINCT {column}) AS n FROM {table} {where}",  # nosec B608 - fixed names
            params,
        )
        return str(row["n"]) if row else "0"

    # ------------------------------------------------------------------
    # Endpoint-specific queries.
    # ------------------------------------------------------------------

    async def employee_by_uid(self, company: str, uid: str) -> Record | None:
        """The generated employee master row for one account (identity source)."""
        rows = await self._db.fetch(
            "SELECT payload FROM mock_employee WHERE company = %(company)s "
            "AND uid = %(uid)s ORDER BY id LIMIT 1",
            {"company": company, "uid": uid},
        )
        return Record(rows[0]["payload"]) if rows else None

    async def login_employee(self, company: str, uid: str | None = None) -> Record | None:
        """Pick the login account for a company.

        With ``uid``, the named generated employee; without one, the company's
        boss (99) — or its first employee when the company has no boss — so
        ``/api/system/token`` stays usable with only the AppKey.
        """
        if uid:
            return await self.employee_by_uid(company, uid)
        for role in (ROLE_BOSS, ROLE_WORKER):
            rows = await self._db.fetch(
                "SELECT payload FROM mock_employee WHERE company = %(company)s "
                "AND payload->>'move_admin_role' = %(role)s ORDER BY uid LIMIT 1",
                {"company": company, "role": role},
            )
            if rows:
                return Record(rows[0]["payload"])
        return None

    async def user_info(self, username: str) -> list[Record]:
        rows = await self._db.fetch(
            "SELECT payload FROM mock_user_info WHERE username = %(username)s ORDER BY id",
            {"username": username},
        )
        return [Record(row["payload"]) for row in rows]

    async def huohao_all(self) -> list[Record]:
        rows = await self._db.fetch("SELECT payload FROM mock_huohao ORDER BY id")
        return [Record(row["payload"]) for row in rows]

    async def huohao_by_bh(self, bh: str) -> list[Record]:
        rows = await self._db.fetch(
            "SELECT payload FROM mock_huohao WHERE bh = %(bh)s ORDER BY id", {"bh": bh}
        )
        return [Record(row["payload"]) for row in rows]

    async def sc_types(self) -> list[Record]:
        rows = await self._db.fetch("SELECT payload FROM mock_sc_type ORDER BY id")
        return [Record(row["payload"]) for row in rows]

    async def rfid_worktypes(self) -> list[Record]:
        rows = await self._db.fetch("SELECT payload FROM mock_rfid_worktype ORDER BY wt_sort")
        return [Record(row["payload"]) for row in rows]

    async def huohao_worktypes(self, huohao: str) -> list[Record]:
        rows = await self._db.fetch(
            "SELECT payload FROM mock_huohao_worktype WHERE huohao = %(huohao)s ORDER BY id",
            {"huohao": huohao},
        )
        return [Record(row["payload"]) for row in rows]

    async def sclzd_worktypes_by_dh(self, identity: Record, dh: str) -> list[Record]:
        return await self.list_rows(
            "mock_sclzd_worktype",
            identity,
            extra=[("dh", "=", dh)],
            order_by="payload->>'sort'",
        )

    async def barcodes_for_detail(self, identity: Record, detail_id: str) -> list[Record]:
        return await self.list_rows(
            "mock_barcode",
            identity,
            extra=[("detail_id", "=", detail_id)],
            order_by="id",
        )

    async def sclzd_by_id(self, identity: Record, userid: str) -> Record | None:
        rows = await self.list_rows("mock_sclzd", identity, extra=[("id", "=", userid)], limit=1)
        return rows[0] if rows else None

    # ------------------------------------------------------------------
    # Wage three-source merge (Type 0 扫码 / 1 吊挂 / 2 手工账).
    # ------------------------------------------------------------------

    async def wage_rows(
        self,
        identity: Record,
        types: set[str],
        start: date,
        end: date,
    ) -> list[Record]:
        """SQL-filter the three sources, then normalise to wage rows."""
        merged: list[Record] = []
        if "0" in types:
            for row in await self.list_rows(
                "mock_barcode_cl",
                identity,
                date_field="rq",
                start=start,
                end=end,
                order_by="rq, id",
            ):
                merged.append(
                    {
                        "id": row["id"],
                        "type": "扫码产量",
                        "rq": row["rq"],
                        "inputtime": row["inputtime"],
                        "uid": row["uid"],
                        "uname": row["uname"],
                        "dept": row["dept"],
                        "chuanghao": row["chuanghao"],
                        "baohao": row["baohao"],
                        "huohao": row["huohao"],
                        "color": row["color"],
                        "chima": row["chima"],
                        "worktype": row["worktype"],
                        "ischeck": 1,
                        "check_time": row["rq"],
                        "fhsl": row["fhsl"],
                        "sl": row["sssl"],
                        "price": row["price"],
                        "je": row["je"],
                        "inputtime_raw": row["inputtime"],
                        "check_time_raw": row["rq"],
                        "_day": row["rq"],
                    }
                )
        if "1" in types:
            for row in await self.list_rows(
                "mock_dg_cl",
                identity,
                date_field="rq",
                start=start,
                end=end,
                order_by="rq, id",
            ):
                merged.append(
                    {
                        "id": row["id"],
                        "type": "吊挂产量",
                        "rq": row["rq"],
                        "inputtime": row["rq"],
                        "uid": row["uid"],
                        "uname": row["uname"],
                        "dept": row["dept"],
                        "chuanghao": row["chuanghao"],
                        "baohao": "包1",
                        "huohao": row["huohao"],
                        "color": row["color"],
                        "chima": row["chima"],
                        "worktype": row["worktype"],
                        "ischeck": 0,
                        "check_time": "",
                        "fhsl": row["sl"],
                        "sl": row["sl"],
                        "price": row["price"],
                        "je": row["je"],
                        "inputtime_raw": row["rq"],
                        "check_time_raw": "",
                        "_day": row["rq"],
                    }
                )
        if "2" in types:
            for row in await self.list_rows(
                "mock_pin_feng",
                identity,
                date_field="zhdate",
                start=start,
                end=end,
                order_by="zhdate, id",
            ):
                merged.append(
                    {
                        "id": row["id"],
                        "type": "手工账产量",
                        "rq": row["zhdate"],
                        "inputtime": row["zhdate"],
                        "uid": row["uid"],
                        "uname": row["uname"],
                        "dept": row["dept"],
                        "chuanghao": row["chuanghao"],
                        "baohao": "包1",
                        "huohao": row["huohao"],
                        "color": row["color"],
                        "chima": row["chima"],
                        "worktype": row["worktype"],
                        "ischeck": 1,
                        "check_time": row["zhdate"],
                        "fhsl": row["js"],
                        "sl": row["sl"],
                        "price": row["price"],
                        "je": row["je"],
                        "inputtime_raw": row["zhdate"],
                        "check_time_raw": row["zhdate"],
                        "_day": row["zhdate"],
                    }
                )
        return merged

    # ------------------------------------------------------------------
    # Table shapes used by paginate().

    @staticmethod
    def wage_source_sum(
        rows: Iterable[Record],
        field: str,
    ) -> str:
        total = sum((_d(row.get(field, "0")) for row in rows), Decimal())
        return str(total)

    @staticmethod
    def distinct_baohao(rows: Iterable[Record]) -> str:
        return str(len({str(row["baohao"]) for row in rows})) if any(True for _ in rows) else "0"


__all__ = ["MockMesStore", "Page"]
