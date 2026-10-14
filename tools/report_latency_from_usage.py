"""从生产埋点 ``usage_event`` 读分段耗时，定位瓶颈在 MES 段还是 LLM 段。

用法（仓库根目录）：

    set -a; source .env; set +a
    .venv/bin/python tools/report_latency_from_usage.py
    .venv/bin/python tools/report_latency_from_usage.py --since 2026-09-01 --until 2026-09-30

**只读**：只对 ``usage_event`` 执行 SELECT，不访问客户 MES，不写任何表。

为什么用埋点而不是现场掐表：``interaction_completed`` 事件同时记录了
``duration_ms`` / ``llm_duration_ms`` / ``mes_duration_ms`` / ``local_duration_ms``，
是同一轮问答内的一致切片；现场分两次跑会把「两遍之间数据变化」算成差异。
"""

from __future__ import annotations

import argparse
import os
import statistics as stats

import psycopg

#: 分段耗时字段（interaction_completed）。
SEGMENTS = (
    ("llm", "llm_duration_ms", "LLM 段（3 次串行调用）"),
    ("mes", "mes_duration_ms", "MES 段（客户接口调用）"),
    ("local", "local_duration_ms", "本地段（DuckDB/聚合/组装）"),
)


def pct(part: float, whole: float) -> str:
    return f"{part / whole * 100:5.1f}%" if whole else "   n/a"


def section(title: str) -> None:
    print(f"\n{'=' * 78}\n{title}\n{'=' * 78}")


def main() -> None:
    parser = argparse.ArgumentParser(description="从 usage_event 读分段耗时并定位瓶颈")
    parser.add_argument("--since", default=None, help="起始日期 YYYY-MM-DD（含）")
    parser.add_argument("--until", default=None, help="结束日期 YYYY-MM-DD（不含）")
    args = parser.parse_args()

    url = os.environ.get("FACTORY_AGENT_POSTGRES_URL")
    if not url:
        raise SystemExit("缺少 FACTORY_AGENT_POSTGRES_URL（先 set -a; source .env; set +a）")

    where, params = ["event_type = 'interaction_completed'"], []
    if args.since:
        where.append("occurred_at >= %s")
        params.append(args.since)
    if args.until:
        where.append("occurred_at < %s")
        params.append(args.until)
    clause = " AND ".join(where)

    with psycopg.connect(url, connect_timeout=8) as con:
        cur = con.cursor()

        section("① 单轮问答分段耗时（interaction_completed）")
        cur.execute(
            f"""
            SELECT (payload->>'duration_ms')::bigint        AS total,
                   (payload->>'llm_duration_ms')::bigint    AS llm,
                   (payload->>'mes_duration_ms')::bigint    AS mes,
                   (payload->>'local_duration_ms')::bigint  AS local,
                   payload->>'result_rows_bucket'           AS bucket,
                   payload->>'status'                       AS status
            FROM usage_event WHERE {clause}
            """,
            params,
        )
        rows = cur.fetchall()
        if not rows:
            print("  该时间窗内没有 interaction_completed 事件。")
            return

        total = [r[0] or 0 for r in rows]
        grand = sum(total)
        # 行布局：[0]=total, [1]=llm, [2]=mes, [3]=local
        segments = [
            (label, [r[i] or 0 for r in rows]) for i, (_, _, label) in enumerate(SEGMENTS, start=1)
        ]

        print(f"  样本 {len(rows)} 轮，时间窗 {args.since or '全部'} ~ {args.until or '现在'}\n")
        print(f"  {'段':34s} {'占Σ总':>7s} {'中位':>9s} {'均值':>9s} {'单次最大':>10s}")
        for label, vals in segments:
            print(
                f"  {label:34s} {pct(sum(vals), grand):>7s} "
                f"{stats.median(vals):8.0f}ms {stats.mean(vals):8.0f}ms {max(vals):9d}ms"
            )
        print(
            f"  {'总耗时':34s} {'100.0%':>7s} "
            f"{stats.median(total):8.0f}ms {stats.mean(total):8.0f}ms {max(total):9d}ms"
        )

        top_label, top_vals = max(segments, key=lambda s: sum(s[1]))
        print(f"\n  → 占比最大：**{top_label}**（{pct(sum(top_vals), grand).strip()}）")

        section("② 单次 MES 调用（mes_call_completed，按均值降序）")
        cur.execute(
            """
            SELECT payload->>'operation_id' AS op, count(*),
                   round(avg((payload->>'duration_ms')::numeric))     AS avg_ms,
                   max((payload->>'duration_ms')::bigint)             AS max_ms,
                   round(avg((payload->>'page_count')::numeric), 1)    AS avg_pages
            FROM usage_event WHERE event_type = 'mes_call_completed'
            GROUP BY 1 ORDER BY avg_ms DESC NULLS LAST
            """
        )
        print(f"  {'operation':28s} {'n':>4s} {'均值':>10s} {'最大':>10s} {'平均页数':>9s}")
        for op, n, avg_ms, max_ms, pages in cur.fetchall():
            print(
                f"  {str(op):28s} {n:4d} {str(avg_ms) + 'ms':>10s} "
                f"{str(max_ms) + 'ms':>10s} {str(pages):>9s}"
            )

        section("③ 单次 LLM 调用（llm_call_completed，按阶段）")
        cur.execute(
            """
            SELECT payload->>'stage' AS stage, count(*),
                   round(avg((payload->>'duration_ms')::numeric)) AS avg_ms,
                   max((payload->>'duration_ms')::bigint)         AS max_ms
            FROM usage_event WHERE event_type = 'llm_call_completed'
            GROUP BY 1 ORDER BY avg_ms DESC NULLS LAST
            """
        )
        llm_total = 0.0
        for stage, n, avg_ms, max_ms in cur.fetchall():
            llm_total += float(avg_ms or 0)
            print(f"  {str(stage):16s} n={n:4d} 均 {avg_ms}ms 最大 {max_ms}ms")
        print(f"\n  → 三段合计约 **{llm_total / 1000:.1f} s**")

        section("④ 按能力切片（interaction_routed × interaction_completed）")
        cur.execute(
            """
            SELECT r.payload->>'capability' AS cap, count(*),
                   round(avg((c.payload->>'duration_ms')::numeric))     AS total,
                   round(avg((c.payload->>'llm_duration_ms')::numeric)) AS llm,
                   round(avg((c.payload->>'mes_duration_ms')::numeric)) AS mes
            FROM usage_event r
            JOIN usage_event c ON r.trace_id = c.trace_id
                              AND c.event_type = 'interaction_completed'
            WHERE r.event_type = 'interaction_routed'
            GROUP BY 1 ORDER BY total DESC NULLS LAST
            """
        )
        print(f"  {'capability':14s} {'n':>4s} {'总':>10s} {'LLM':>10s} {'MES':>10s}")
        for cap, n, total_ms, llm_ms, mes_ms in cur.fetchall():
            print(
                f"  {str(cap):14s} {n:4d} {str(total_ms) + 'ms':>10s} "
                f"{str(llm_ms) + 'ms':>10s} {str(mes_ms) + 'ms':>10s}"
            )


if __name__ == "__main__":
    main()
