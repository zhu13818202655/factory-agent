"""Interaction-scoped read-only DuckDB sandbox.

Each interaction creates an isolated in-memory DuckDB connection, registers
only validated and authorized data, executes reviewed SELECT/WITH SQL against
a table whitelist, and is destroyed afterward. File reads, external scans,
extension loading, ``ATTACH``, ``COPY``, DDL, and DML are all blocked.
"""

import json
import re
from dataclasses import dataclass
from typing import Any, Mapping, Self, Sequence

import duckdb

from factory_agent.domain.errors import ForbiddenError, InvalidRequestError
from factory_agent.execution.sandbox import InteractionSandboxPolicy

# Statements that must never execute inside the sandbox.
_FORBIDDEN_PREFIXES: tuple[str, ...] = (
    "ATTACH",
    "DETACH",
    "COPY",
    "CREATE",
    "DROP",
    "ALTER",
    "INSERT",
    "UPDATE",
    "DELETE",
    "PRAGMA",
    "EXPORT",
    "IMPORT",
    "INSTALL",
    "LOAD",
    "CALL",
    "SET",
    "RESET",
    "CHECKPOINT",
    "VACUUM",
    "BEGIN",
    "COMMIT",
    "ROLLBACK",
)

_FORBIDDEN_TOKENS: tuple[str, ...] = (
    "READ_CSV",
    "READ_JSON",
    "READ_PARQUET",
    "PARQUET_SCAN",
    "CSV_READ",
    "GLOB(",
    "SNIPPET(",
    "PG_",
    "MYSQL_",
    "SQLITE_SCAN",
    "ICEBERG_",
    "HTTPFS",
)


@dataclass(frozen=True, slots=True)
class SandboxTable:
    """One authorized table registered for this interaction only."""

    name: str
    rows: tuple[dict[str, Any], ...]
    columns: tuple[tuple[str, str], ...]


#: Column types the bulk insert can carry through a JSON STRUCT cast. The
#: kernel only ever registers these; anything else takes the per-row path.
_BULK_SQL_TYPES: frozenset[str] = frozenset(
    {"VARCHAR", "INTEGER", "BIGINT", "DOUBLE", "BOOLEAN", "DATE", "TIMESTAMP"}
)
_DECIMAL_TYPE = re.compile(r"^DECIMAL\(\d{1,2},\s*\d{1,2}\)$")


def _bulk_capable_type(sql_type: str) -> bool:
    return sql_type in _BULK_SQL_TYPES or _DECIMAL_TYPE.match(sql_type) is not None


def _validate_identifier(name: str) -> str:
    if not name or not name.replace("_", "").isalnum():
        raise InvalidRequestError("table names must be alphanumeric or underscore")
    return name


class InteractionSandbox:
    """One in-memory DuckDB connection bound to a single interaction."""

    def __init__(
        self,
        policy: InteractionSandboxPolicy | None = None,
        allowed_tables: Sequence[str] | None = None,
    ) -> None:
        self._policy = policy or InteractionSandboxPolicy()
        self._allowed_tables = frozenset(
            _validate_identifier(name) for name in (allowed_tables or ())
        )
        self._connection: duckdb.DuckDBPyConnection | None = None
        self._registered: set[str] = set()
        self._closed = False

    def _conn(self) -> duckdb.DuckDBPyConnection:
        if self._closed:
            raise InvalidRequestError("sandbox connection was destroyed after this interaction")
        if self._connection is None:
            config: dict[str, str | bool | int | float | list[str]] = {
                "allow_unsigned_extensions": False,
                "enable_external_access": False,
                "lock_configuration": True,
            }
            self._connection = duckdb.connect(database=":memory:", config=config)
        return self._connection

    def register_table(self, table: SandboxTable) -> None:
        """Register validated rows; the whitelist gates which names are legal."""
        name = _validate_identifier(table.name)
        if self._allowed_tables and name not in self._allowed_tables:
            raise ForbiddenError("table name is not in the reviewed whitelist")
        connection = self._conn()
        columns_sql = ", ".join(f'"{column}" {sql_type}' for column, sql_type in table.columns)
        # Identifiers are validated above; no user-controlled text enters this DDL.
        connection.execute(f'CREATE TABLE "{name}" ({columns_sql})')  # nosec B608
        if table.rows:
            if not self._register_rows_bulk(connection, name, table):
                self._register_rows_per_row(connection, name, table)
        self._registered.add(name)

    def _register_rows_bulk(
        self, connection: duckdb.DuckDBPyConnection, name: str, table: SandboxTable
    ) -> bool:
        """Insert every row in one statement via a JSON STRUCT cast.

        One parameterised ``execute`` per row costs ~1.4 ms of client-side
        binding each (measured on duckdb 1.5.5), so a 75k-row MES page spent
        ~110 s of pure CPU inside the event loop between two MES calls — time
        the trace ledger then misreports as upstream latency. Casting one JSON
        document into a STRUCT array and unnesting it inserts the same rows in
        well under a second.

        Returns ``False`` — the caller falls back to the per-row insert — when
        a column type or value cannot go through the JSON round-trip. The
        single INSERT is atomic, so a failed attempt leaves no partial rows
        behind before the fallback runs. Column names here are the same
        reviewed identifiers the CREATE TABLE above already used, and the type
        spec is built from the reviewed vocabulary only.
        """
        columns = list(table.columns)
        if any(not _validate_identifier(column) for column, _ in columns):
            return False
        if any(not _bulk_capable_type(sql_type) for _, sql_type in columns):
            return False
        names = [column for column, _ in columns]
        try:
            payload = json.dumps(
                [{column: row.get(column) for column in names} for row in table.rows],
                ensure_ascii=False,
            )
        except (TypeError, ValueError):
            # A value JSON cannot carry (e.g. a non-finite float): per-row
            # binding handles whatever coercion DuckDB can do natively.
            return False
        struct = (
            "STRUCT("
            + ", ".join(f'"{column}" {sql_type}' for column, sql_type in columns)
            + ")[]"
        )
        try:
            connection.execute(
                f'INSERT INTO "{name}" '  # nosec B608 - identifiers reviewed above
                f"SELECT s.* FROM (SELECT unnest(CAST(? AS {struct})) AS s)",
                [payload],
            )
        except Exception:  # noqa: BLE001 - any cast failure degrades to per-row
            return False
        return True

    def _register_rows_per_row(
        self, connection: duckdb.DuckDBPyConnection, name: str, table: SandboxTable
    ) -> None:
        """The original one-execute-per-row insert, kept as the fallback."""
        column_names = [column for column, _ in table.columns]
        placeholders = ", ".join("?" for _ in column_names)
        # Identifiers are validated above and values are bound parameters;
        # no user-controlled text enters this statement.
        insert_sql = (
            f'INSERT INTO "{name}" '  # nosec B608 - identifiers whitelisted
            f"({', '.join(chr(34) + c + chr(34) for c in column_names)}) "
            f"VALUES ({placeholders})"
        )
        for row in table.rows:
            params = tuple(row.get(column) for column, _ in table.columns)
            connection.execute(insert_sql, params)

    def execute(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> list[tuple[Any, ...]]:
        """Run one reviewed read-only statement and fetch all rows.

        Named parameters are passed through as a dict so reviewed local compute
        can reference ``$order_codes``-style bindings (user business
        filters); positional sequences are passed as a list. Values are always
        bound parameters — never interpolated into the SQL text.
        """
        _columns, rows = self.execute_typed(sql, params)
        return rows

    def execute_typed(
        self,
        sql: str,
        params: Mapping[str, Any] | Sequence[Any] | None = None,
    ) -> tuple[tuple[str, ...], list[tuple[Any, ...]]]:
        """``execute`` plus the result column names from the cursor description.

        Aux outputs (card-only chart/KPI series) declare their columns in the
        reviewed SQL itself, so the runtime column names come from DuckDB —
        never from user input.
        """
        self._assert_read_only(sql)
        connection = self._conn()
        try:
            if params is None:
                result = connection.execute(sql)
            elif isinstance(params, Mapping):
                result = connection.execute(sql, dict(params))
            else:
                result = connection.execute(sql, list(params))
            rows: list[tuple[Any, ...]] = result.fetchall()
            columns = tuple(str(item[0]) for item in (result.description or ()))
        except duckdb.Error as error:
            raise InvalidRequestError(f"sandbox rejected the statement: {error}") from error
        return columns, rows

    def execute_df(self, sql: str) -> duckdb.DuckDBPyConnection:
        """Run a reviewed query and return the connection for result consumption."""
        self._assert_read_only(sql)
        return self._conn().execute(sql)

    @staticmethod
    def _assert_read_only(sql: str) -> None:
        stripped = sql.strip().rstrip(";").strip()
        upper = stripped.upper()
        first_word = upper.split(None, 1)[0] if upper else ""
        if first_word not in ("SELECT", "WITH", "DESCRIBE", "SHOW", "EXPLAIN"):
            raise ForbiddenError("only SELECT/WITH statements may run in the sandbox")
        for prefix in _FORBIDDEN_PREFIXES:
            if upper.startswith(prefix):
                raise ForbiddenError(f"statement type {prefix} is blocked in the sandbox")
        for token in _FORBIDDEN_TOKENS:
            if token in upper:
                raise ForbiddenError("external access functions are blocked in the sandbox")

    def close(self) -> None:
        """Destroy the connection; all registered data dies with it."""
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        self._registered.clear()
        self._closed = True

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()


__all__ = ["InteractionSandbox", "SandboxTable"]
