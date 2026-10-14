# factory-agent

`factory-agent` is a read-only factory MES assistant. The customer MES contract is documented in
[docs/product/AI问答对外接口-整理.md](docs/product/AI问答对外接口-整理.md); product requirements
and confirmed customer answers live in [docs/product/需求及方案整理.md](docs/product/需求及方案整理.md).

## Start here

- [Product requirements](docs/product/需求及方案整理.md): authoritative functional and quality scope.
- [Repository rules](AGENTS.md): architecture boundaries and development conventions.

Superseded requirements and customer documents stay recoverable through git history and are not kept
as live provenance copies.

## Services

One buildable unit lives in this repository, and one process serves two surfaces:

- `factory-agent` (repository root): the read-only MES assistant. It also writes usage metering
  (`usage_event`, the `*_fact` tables, `mes_operation_category`, `tenant_usage_*`) directly into
  PostgreSQL in a separate transaction after its business commit — no outbox, no publisher, no
  cross-service usage-event contract. A metering failure is alerted and never rolls back or blocks
  an answer.
- `src/factory_agent/statistics/` mounted at `/v1/statistics`: the platform statistics surface for
  usage dashboards, tenant lifecycle, and exports. It owns and writes `tenant_registry`,
  `admin_audit`, `platform_principal`, and `usage_export`; every other table is read-only for it. It
  shares no engine or connection pool with the business pipeline.

The whole database is built by a single Alembic baseline (one `alembic_version`).

## Exports and object storage

Exports are generated on demand and retained on disk (never held only in memory), with the
customer-confirmed 90-day default retention window and lazy cleanup on access. Both surfaces write
their artifacts to the same S3-compatible gateway but keep separate buckets, and each serves
downloads through its own backend-proxied endpoint: the service re-checks ownership, writes an
audit event, and refuses to release bytes when that audit fails (fail-closed).

| Surface | Backend selection | Default bucket |
|---|---|---|
| business artifacts | `FACTORY_AGENT_S3_ENDPOINT_URL` set → S3; empty → `FACTORY_AGENT_EXPORT_STORE_DIR` local directory | `factory-agent-exports` |
| statistics reports | `FACTORY_AGENT_STATISTICS_S3_ENDPOINT_URL` set → S3; empty → `FACTORY_AGENT_STATISTICS_EXPORT_STORE_DIR`; both empty → memory (dev/test only, lost on restart) | `factory-agent-statistics-exports` |

The reference deployment uses single-node SeaweedFS; `make middleware-up` starts it alongside
PostgreSQL and Redis, and `make compose-up` starts the complete stack. See
[ADR-0009](docs/adr/0009-export-artifact-storage-backend.md) for the decisions behind the storage
split and [deploy/compose/README.md](deploy/compose/README.md) for the deployment details.

## Development

```bash
make bootstrap
make check
make dev
```

Both surfaces expose `GET /health/live` and `GET /health/ready` from the same application.

For container-based local debugging, start PostgreSQL and Redis with `make middleware-up`, then
start the complete application stack with `make compose-up`. See
[deploy/compose/README.md](deploy/compose/README.md) for ports, databases, and cleanup commands.

## 批量角色问答测试

`tests/data/qa_questions.csv` 保存角色与问题，`scripts/run_qa_batch.py` 通过正常的 HTTP/SSE
接口执行问答。每个角色选择对应的加密凭据，实际身份、权限、MES 查询和模型调用均由应用完成。
脚本不传角色、租户或用户字段来覆盖服务端授权。

根目录 `.env` 支持 `MES_USER_CREDENTIAL_00/01/02/99`；也兼容带 `FACTORY_AGENT_` 前缀的名称。
两种名称同时存在时，批量客户端优先使用不带前缀的值。公共缓存生产者仍使用应用既有的
`FACTORY_AGENT_MES_USER_CREDENTIAL_99`。CSV 和输出文件均不保存凭据。

使用远程中间件时，先将 `.env` 中各连接地址的 `REMOTE_SERVER_IP` 替换为实际地址。
首次使用空数据库时，在项目根目录加载配置并迁移，然后启动本地 API：

```bash
make qa-migrate
make qa-api
```

在另一个终端运行：

```bash
# 仅检查 CSV 和角色配置，不发送请求
make qa-check

# 先试少量问题
make qa QA_LIMIT=5

# 完整执行两轮，保留实际缓存来源及耗时供比较
make qa QA_REPEAT=2

# 可选：只测试指定角色，或连接已运行的 API
make qa QA_ROLES="01 02"
make qa QA_BASE_URL=http://127.0.0.1:8080
```

`make qa` 默认执行一轮。可用 `QA_CSV`、`QA_ENV_FILE`、`QA_TIMEOUT`、`QA_OUTPUT_DIR`
分别指定问题文件、配置文件、单问题限时和空的结果目录；`QA_HOST`、`QA_PORT` 设置本地 API
监听地址与端口，默认批量客户端地址与其一致。`QA_LIMIT` 在角色筛选后限制总问题数。
以上 QA 入口使用已配置的中间件，`qa-migrate` 不会启动本地 Docker 中间件。

如果 Windows 下 uv 的默认缓存目录不可写，使用 `make qa-check UV="uv --cache-dir .cache/uv"`，
其余 QA 命令同样可带该参数。其他仓库 Make 任务使用 Bash；直接运行批量脚本也支持 PowerShell：

```powershell
uv run --no-sync python scripts/run_qa_batch.py --dry-run
uv run --no-sync python scripts/run_qa_batch.py --repeat 2
```

应用进程读取的 `FACTORY_AGENT_RESULT_CACHE_MODE=readwrite` 才是缓存开关；修改配置后重启 API。
Windows 下使用 `make qa-api` 或加载 `.env` 后的 `factory-agent` 入口启动，它会为 Psycopg
异步 PostgreSQL 连接选择兼容的 SelectorEventLoop。直接使用 Uvicorn CLI 的默认事件循环
可能导致 `ProactorEventLoop` 兼容错误；程序输出正在监听端口时，后台数据库任务仍可能失败。

CSV 必需列为 `role`、`question`，支持中文角色名、`00/01/02/99` 或英文角色名。
模板另含 `case_id`、`category`、`expected_capability`、`expected_outcome`、`session_group`。
相同角色及 `session_group` 的问题在同一会话内依次执行，用于追问；不同角色及重复轮次的会话
独立。空白分组表示独立问题。预期状态用 `|` 分隔可接受值，留空表示人工评估。

初始 80 个问题覆盖个人查询、管理查询、老板查询、权限拒绝、缺参澄清、闲聊、追问、时间边界和
复杂分析。没有捏造真实订单或员工编号；订单及员工查询中的缺参用例测试澄清流程，具体目标的
业务执行仍需另加合法测试问题。

每次运行在 `data/qa-runs/<时间戳>-<运行标识>/` 保存：

- `results.csv`：逐问题回答、澄清、实际能力、状态、行数、完整性、缓存来源及客户端耗时。
- `results.jsonl`：同一结果的机器可读版本，另含结果卡片、事件名称和可用的 trace 汇总。
- `summary.json`：按角色统计的状态分布、预期匹配及 p50/p95 耗时。
- `manifest.json`：运行时间、输入及脚本摘要、所选问题、轮次和配置角色，供复现与对照。

`answer`、`empty`、`incomplete`、`clarification`、`denied`、`rejected`、`chat`、`failed`、
`cancelled`、`skipped` 分别记录回答、空结果、不完整结果、澄清、权限拒绝、时间范围拒绝、
闲聊、故障、取消和缺少凭据。预期匹配仅是状态及可观测能力的检查，不是业务数值正确率。
具体数值、口径、权限范围、卡片和回答质量需要根据结果另行核对。

脚本顺序执行，每个问题完成即写入结果；断流只续读同一 interaction，不重复提交问题。
单问题默认限时 360 秒，超时尝试取消该交互并继续后续用例。异常和输出中的已配置秘密值会被
遮盖，控制台只打印用例标识、角色、状态和耗时。输出目录使用忽略的运行数据目录。

trace 不可用时只标记其状态，不影响已得到的回答。记录的 span 数量不等于完整的实际调用数；
重复轮次也不自动证明缓存命中，须检查 `cache_source` 与可用的缓存指标。脚本不会清理共享缓存。
退出码 `0` 表示所执行的状态检查无失败，`1` 表示故障、取消、未执行或预期不匹配，
`2` 表示配置或文件错误，`130` 表示人工中止。

## Submodules

`frontend/` and `frontend-h5/` are third-party Git submodules owned by other teams. This
repository only pins a commit pointer for each. After a fresh clone, initialise them with
`git submodule update --init --recursive`. When an upstream frontend changes, update the pointer:

```bash
git submodule update --remote frontend      # or: frontend-h5
git add frontend                            # or: frontend-h5
git commit -m "chore: bump frontend submodule to latest"
```
