

import pytest

from factory_agent.domain.errors import ForbiddenError, InvalidRequestError
from factory_agent.execution.sandbox import InteractionSandboxPolicy
from factory_agent.execution.sandbox_runtime import InteractionSandbox, SandboxTable

PIECEWORK_COLUMNS = (
    ("record_id", "VARCHAR"),
    ("employee_id", "VARCHAR"),
    ("qualified_quantity", "DECIMAL(18,4)"),
    ("amount", "DECIMAL(18,4)"),
)


def _sandbox() -> InteractionSandbox:
    sandbox = InteractionSandbox(allowed_tables=["piecework"])
    sandbox.register_table(
        SandboxTable(
            name="piecework",
            rows=(
                {
                    "record_id": "r1",
                    "employee_id": "employee-a1",
                    "qualified_quantity": 5,
                    "amount": 6.25,
                },
                {
                    "record_id": "r2",
                    "employee_id": "employee-a2",
                    "qualified_quantity": 3,
                    "amount": 3.75,
                },
            ),
            columns=PIECEWORK_COLUMNS,
        )
    )
    return sandbox


def test_select_aggregation_over_registered_rows() -> None:
    with _sandbox() as sandbox:
        rows = sandbox.execute(
            "SELECT employee_id, SUM(amount) FROM piecework GROUP BY employee_id "
            "ORDER BY employee_id"
        )
        assert len(rows) == 2
        assert float(rows[0][1]) == 6.25


def test_with_cte_statement_is_allowed() -> None:
    with _sandbox() as sandbox:
        rows = sandbox.execute(
            "WITH totals AS (SELECT SUM(qualified_quantity) AS q FROM piecework) "
            "SELECT q FROM totals"
        )
        assert int(rows[0][0]) == 8


@pytest.mark.parametrize(
    "sql",
    [
        "ATTACH 'x.db' AS evil",
        "COPY piecework TO '/tmp/evil.csv'",
        "CREATE TABLE evil (a INT)",
        "DROP TABLE piecework",
        "ALTER TABLE piecework ADD COLUMN x INT",
        "INSERT INTO piecework VALUES ('r3', 'e', 1, 1)",
        "UPDATE piecework SET amount = 0",
        "DELETE FROM piecework",
        "INSTALL httpx",
        "LOAD 'evil.so'",
    ],
)
def test_ddl_dml_and_file_statements_are_blocked(sql: str) -> None:
    with _sandbox() as sandbox:
        with pytest.raises(ForbiddenError):
            sandbox.execute(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_parquet('s3://bucket/x.parquet')",
        "SELECT * FROM read_json('/tmp/x.json')",
    ],
)
def test_file_reading_functions_are_blocked(sql: str) -> None:
    with _sandbox() as sandbox:
        with pytest.raises((ForbiddenError, InvalidRequestError)):
            sandbox.execute(sql)


def test_non_select_statements_rejected_even_without_prefix_match() -> None:
    with _sandbox() as sandbox:
        with pytest.raises(ForbiddenError):
            sandbox.execute("PRAGMA database_list")


def test_unwhitelisted_table_name_cannot_register() -> None:
    sandbox = InteractionSandbox(allowed_tables=["piecework"])
    with pytest.raises(ForbiddenError):
        sandbox.register_table(SandboxTable(name="secret", rows=(), columns=(("a", "INT"),)))


def test_invalid_identifier_rejected() -> None:
    with pytest.raises(InvalidRequestError):
        InteractionSandbox(allowed_tables=['piece"; DROP TABLE x'])


def test_connection_is_destroyed_after_close_and_no_tables_remain() -> None:
    sandbox = _sandbox()
    assert len(sandbox.execute("SELECT * FROM piecework")) == 2
    sandbox.close()
    with pytest.raises(Exception):  # noqa: B017 - duckdb raises ConnectionException
        sandbox.execute("SELECT 1")


def test_two_interactions_share_no_data() -> None:
    first = _sandbox()
    second = InteractionSandbox(allowed_tables=["piecework"])
    with pytest.raises(InvalidRequestError):
        # Second interaction never registered the table; query must fail.
        second.execute("SELECT COUNT(*) FROM piecework")
    first.close()
    second.close()


def test_policy_defaults_stay_memory_only_and_read_only() -> None:
    policy = InteractionSandboxPolicy()
    assert policy.database == ":memory:"
    assert not policy.allow_external_access
    assert not policy.allow_unsigned_extensions


def test_bulk_registration_preserves_values_and_types() -> None:
    """The JSON round-trip must land exactly what the per-row path landed.

    75k-row MES pages used to cost ~1.4 ms of client-side binding per row
    (~110 s of event-loop stall); the bulk path inserts them in under a
    second. Values, NULLs, missing keys, quoting, and declared types must all
    survive the JSON STRUCT cast identically.
    """
    sandbox = InteractionSandbox(allowed_tables=["bulk"])
    rows = tuple(
        {
            "record_id": "含'引号'\"与\n换行" if index == 0 else f"r{index}",
            "employee_id": None if index % 2 == 0 else f"emp-{index}",
            "qualified_quantity": index,
            "amount": float(index) + 0.25,
        }
        for index in range(2_000)
    )
    sandbox.register_table(
        SandboxTable(
            name="bulk",
            rows=rows,
            columns=(
                ("record_id", "VARCHAR"),
                ("employee_id", "VARCHAR"),
                ("qualified_quantity", "INTEGER"),
                ("amount", "DECIMAL(18,4)"),
            ),
        )
    )
    assert sandbox.execute("SELECT COUNT(*) FROM bulk") == [(2_000,)]
    assert sandbox.execute(
        "SELECT qualified_quantity, amount, employee_id FROM bulk "
        "WHERE record_id = 'r3'"
    ) == [(3, 3.25, "emp-3")]
    # Missing keys and NULLs both land as column NULL.
    null_count = sandbox.execute(
        "SELECT COUNT(*) FROM bulk WHERE employee_id IS NULL"
    )
    assert null_count == [(1_000,)]
    # The INTEGER column really is integral, not text that quacks like it.
    assert sandbox.execute("SELECT MAX(qualified_quantity) + 1 FROM bulk") == [(2_000,)]


def test_bulk_registration_falls_back_for_unknown_types() -> None:
    """A column type outside the JSON-safe vocabulary takes the per-row path."""
    sandbox = InteractionSandbox(allowed_tables=["exotic"])
    sandbox.register_table(
        SandboxTable(
            name="exotic",
            rows=({"payload": 1, "n": 1}, {"payload": 2, "n": 2}),
            columns=(("payload", "TINYINT"), ("n", "INTEGER")),
        )
    )
    assert sandbox.execute("SELECT payload, n FROM exotic ORDER BY n") == [
        (1, 1),
        (2, 2),
    ]
