from datetime import datetime, timezone

import pytest
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from factory_agent.persistence import queries
from factory_agent.persistence.tables import METADATA, message_table

DIALECT = postgresql.dialect()
NOW = datetime(2026, 8, 24, 6, 0, tzinfo=timezone.utc)
TENANT = "tenant-a"
USER = "user-a"
SESSIONS = ("s-1", "s-2")

# ``ClauseElement`` rather than ``Executable``: only the former exposes ``compile``.
Statement = sa.sql.ClauseElement


def compiled(statement: Statement) -> str:
    return str(statement.compile(dialect=DIALECT, compile_kwargs={"literal_binds": True}))


OWNERSHIP_STATEMENTS: dict[str, Statement] = {
    "select_interaction": queries.select_interaction(TENANT, USER, "i-1"),
    "select_events": queries.select_events(TENANT, USER, "i-1", 0),
    "select_messages": queries.select_messages(TENANT, USER, "s-1", 50),
    "select_latest_message": queries.select_latest_message(TENANT, USER, "s-1", ("result_table",)),
    "select_interactions": queries.select_interactions(TENANT, USER, "s-1", 50),
    "select_conversations": queries.select_conversations(TENANT, USER, 20),
    "select_conversation": queries.select_conversation(TENANT, USER, "s-1"),
    "count_conversations": queries.count_conversations(TENANT, USER),
    "select_conversation_messages": queries.select_conversation_messages(TENANT, USER, SESSIONS),
    "select_conversation_first_questions": queries.select_conversation_first_questions(
        TENANT, USER, SESSIONS
    ),
    "select_conversation_interactions": queries.select_conversation_interactions(
        TENANT, USER, SESSIONS
    ),
    "touch_conversation": queries.touch_conversation(TENANT, USER, "s-1", now=NOW),
    "claim_interaction_run": queries.claim_interaction_run(TENANT, USER, "i-1", NOW),
    "fail_stale_interaction_run": queries.fail_stale_interaction_run(
        TENANT, USER, "i-1", stale_before=NOW, now=NOW, category="executor_lost"
    ),
    "delete_session": queries.delete_session(TENANT, USER, "s-1"),
}


def test_every_ownership_scoped_builder_is_covered() -> None:
    assert set(queries.OWNERSHIP_SCOPED_BUILDERS) == set(OWNERSHIP_STATEMENTS)


@pytest.mark.parametrize("name", sorted(OWNERSHIP_STATEMENTS))
def test_business_statements_always_filter_by_trusted_tenant_and_user(name: str) -> None:
    sql = compiled(OWNERSHIP_STATEMENTS[name])

    assert f"tenant_id = '{TENANT}'" in sql
    assert f"user_id = '{USER}'" in sql


@pytest.mark.parametrize("name", sorted(OWNERSHIP_STATEMENTS))
def test_ownership_predicates_are_conjunctive_not_optional(name: str) -> None:
    sql = compiled(OWNERSHIP_STATEMENTS[name]).lower()

    assert " or " not in sql


def test_cursor_pagination_is_stable_and_over_fetches_one_row() -> None:
    sql = compiled(queries.select_messages(TENANT, USER, "s-1", 25))

    assert "ORDER BY" in sql
    assert "created_at ASC" in sql
    assert "interaction_id ASC" in sql
    assert "sequence ASC" in sql
    # message_id 是随机 UUID，永远不允许出现在排序键里。
    order_by = sql.split("ORDER BY", 1)[1]
    assert "message_id" not in order_by
    assert "LIMIT 26" in sql


def test_cursor_round_trips() -> None:
    """轮次与会话列表仍是二元组游标（时间, 行标识）."""
    cursor = queries.encode_cursor(NOW, "s-9")

    assert queries.decode_cursor(cursor) == (NOW, "s-9")


def test_message_cursor_round_trips() -> None:
    cursor = queries.encode_message_cursor(NOW, "i-1", 7)

    assert queries.decode_message_cursor(cursor) == (NOW, "i-1", 7)


@pytest.mark.parametrize("cursor", ["", "not-base64!!", "e30=", "eyJhdCI6IDF9"])
def test_malformed_message_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(queries.CursorError):
        queries.decode_message_cursor(cursor)


def test_legacy_message_cursor_is_rejected() -> None:
    """旧版二元组游标（含随机 message_id）在新排序语义下不可续页，必须显式报错."""
    legacy = queries.encode_cursor(NOW, "m-1")

    with pytest.raises(queries.CursorError):
        queries.decode_message_cursor(legacy)


@pytest.mark.parametrize("cursor", ["", "not-base64!!", "e30=", "eyJhdCI6IDF9"])
def test_malformed_cursors_are_rejected(cursor: str) -> None:
    with pytest.raises(queries.CursorError):
        queries.decode_cursor(cursor)


def test_message_cursor_narrows_the_result_window() -> None:
    sql = compiled(queries.select_messages(TENANT, USER, "s-1", 25, (NOW, "i-1", 7)))

    assert "'i-1'" in sql
    assert "7" in sql
    assert f"tenant_id = '{TENANT}'" in sql


def test_fail_stale_run_requires_running_and_staleness() -> None:
    sql = compiled(
        queries.fail_stale_interaction_run(
            TENANT, USER, "i-1", stale_before=NOW, now=NOW, category="executor_lost"
        )
    )

    assert "status = 'running'" in sql
    assert "updated_at < '2026-08-24 06:00:00+00:00'" in sql
    assert "status='failed'" in sql
    assert "last_event_sequence + 1" in sql


def test_fail_abandoned_runs_targets_unclaimed_pending_only() -> None:
    sql = compiled(
        queries.fail_abandoned_interaction_runs(abandoned_before=NOW, now=NOW, category="abandoned")
    )

    assert "status = 'pending'" in sql
    assert "created_at < '2026-08-24 06:00:00+00:00'" in sql
    assert "status='failed'" in sql
    assert "last_event_sequence + 1" in sql


def test_bulk_recovery_builders_are_deliberately_not_ownership_scoped() -> None:
    """Sweeps repair every owner's rows; they must never be listed as scoped."""

    assert "fail_stale_interaction_runs" not in queries.OWNERSHIP_SCOPED_BUILDERS
    assert "fail_abandoned_interaction_runs" not in queries.OWNERSHIP_SCOPED_BUILDERS
    for statement in (
        queries.fail_stale_interaction_runs(stale_before=NOW, now=NOW, category="executor_lost"),
        queries.fail_abandoned_interaction_runs(
            abandoned_before=NOW, now=NOW, category="abandoned"
        ),
    ):
        # The columns still appear in RETURNING; the point is that no ownership
        # predicate narrows the recovery job to a single caller.
        assert f"tenant_id = '{TENANT}'" not in compiled(statement)
        assert f"user_id = '{USER}'" not in compiled(statement)


def test_message_owner_index_matches_the_read_ordering_key() -> None:
    """索引顺序必须与 ``select_messages`` 的排序键一致，随机 message_id 不进索引."""
    index = {
        str(index.name): [column.name for column in index.columns]
        for index in message_table.indexes
    }

    assert index["agent_message_owner_idx"] == [
        "tenant_id",
        "user_id",
        "session_id",
        "created_at",
        "interaction_id",
        "sequence",
    ]


def test_messages_and_events_cascade_from_the_interaction() -> None:
    cascading = {
        table.name: {constraint.ondelete for constraint in table.foreign_key_constraints}
        for table in METADATA.tables.values()
        if table.foreign_key_constraints
    }

    assert cascading["agent_message"] == {"CASCADE"}
    assert cascading["agent_interaction_event"] == {"CASCADE"}


def test_message_sequence_is_unique_within_an_interaction() -> None:
    unique = {
        tuple(sorted(column.name for column in constraint.columns))
        for constraint in message_table.constraints
        if isinstance(constraint, sa.UniqueConstraint)
    }

    assert ("interaction_id", "sequence") in unique


def test_latest_message_reads_newest_first_one_row_within_the_given_kinds() -> None:
    """窗口兜底取的是「最近一条结果消息」：倒序一行，且只认指定 kind（D-7）."""
    sql = compiled(queries.select_latest_message(TENANT, USER, "s-1", ("error", "result_table")))

    assert "created_at DESC" in sql
    assert "interaction_id DESC" in sql
    assert "sequence DESC" in sql
    assert "LIMIT 1" in sql
    assert "kind IN ('error', 'result_table')" in sql


def test_conversation_page_is_newest_first_with_a_one_row_over_fetch() -> None:
    sql = compiled(queries.select_conversations(TENANT, USER, 20))

    assert "updated_at DESC" in sql
    assert "session_id DESC" in sql
    assert "LIMIT 21" in sql


def test_conversation_cursor_walks_strictly_before_the_last_seen_key() -> None:
    """倒序翻页的游标谓词必须是 `<`，用 `>` 会跳过整页并把首页重复一遍."""
    sql = compiled(queries.select_conversations(TENANT, USER, 20, (NOW, "s-9")))

    assert "< ('2026-08-24 06:00:00+00:00', 's-9')" in sql
    assert f"tenant_id = '{TENANT}'" in sql


def test_conversation_hydration_is_one_statement_per_source_table() -> None:
    """列表水合必须是按 session 分组的聚合，不能每个会话查一次（N+1）."""
    messages = compiled(queries.select_conversation_messages(TENANT, USER, SESSIONS))
    turns = compiled(queries.select_conversation_interactions(TENANT, USER, SESSIONS))

    # DISTINCT ON 取每会话最新一行，窗口计数在同一语句里给出该会话的条数。
    assert "DISTINCT ON (agent_message.session_id)" in messages
    assert "count(*) OVER (PARTITION BY agent_message.session_id)" in messages
    assert "session_id IN ('s-1', 's-2')" in messages
    assert "DISTINCT ON (agent_interaction.session_id)" in turns
    assert "count(*) OVER (PARTITION BY agent_interaction.session_id)" in turns


def test_conversation_preview_and_counts_skip_phase_rows() -> None:
    """过程行不进预览也不计数：它在历史里没有回看价值."""
    messages = compiled(queries.select_conversation_messages(TENANT, USER, SESSIONS))

    assert "kind != 'phase'" in messages


def test_title_source_is_the_earliest_user_question_not_the_newest() -> None:
    sql = compiled(queries.select_conversation_first_questions(TENANT, USER, SESSIONS))

    assert "created_at ASC" in sql
    assert "interaction_id ASC" in sql
    assert "sequence ASC" in sql
    assert "role = 'user'" in sql
    assert "kind = 'plain_text'" in sql


def test_touch_conversation_never_moves_recency_backwards() -> None:
    """迟到的提交不能把会话从列表顶部挤下去，所以是 CASE 取较大值."""
    sql = compiled(queries.touch_conversation(TENANT, USER, "s-1", now=NOW))

    assert "CASE WHEN" in sql
    assert "updated_at >" in sql


def test_message_exclusion_is_applied_in_sql_and_keeps_ownership() -> None:
    sql = compiled(queries.select_messages(TENANT, USER, "s-1", 50, None, ("phase",)))

    assert "kind NOT IN ('phase')" in sql
    assert f"tenant_id = '{TENANT}'" in sql
    assert f"user_id = '{USER}'" in sql


def test_insert_conversation_writes_the_trusted_ownership_pair() -> None:
    """插入语句没有 WHERE 谓词可守，所以单独证明它写入的是可信归属对.

    它因此**不**登记在 ``OWNERSHIP_SCOPED_BUILDERS`` 里（见下一条测试）。
    """
    sql = compiled(queries.insert_conversation(TENANT, USER, "s-1", created_at=NOW))

    assert "INSERT INTO agent_conversation" in sql
    assert f"('{TENANT}', '{USER}', 's-1'" in sql
    assert "ON CONFLICT (tenant_id, user_id, session_id) DO NOTHING" in sql


def test_insert_conversation_is_deliberately_not_ownership_scoped() -> None:
    """插入写值而不是过滤行，把它登记为 scoped 会让那条不变量名不副实."""
    assert "insert_conversation" not in queries.OWNERSHIP_SCOPED_BUILDERS
