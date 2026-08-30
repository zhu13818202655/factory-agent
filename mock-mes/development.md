# mock-mes 开发指南

模拟客户 MES 的确定性服务，用于本地开发/测试/演示，**不是生产依赖**。自 Story 10 起，
数据完全由 `(seed, day)` 与工厂规模参数（`headcount`/`departments`/`group_size` 等）决定并**持久化到 PostgreSQL**：生成器进程写库，API
进程只读，PG 是唯一数据源（无内存数据集、无内存回退）。

## 快速开始

```bash
# 0. 准备 PostgreSQL（例如仓库 Compose 的 postgres 服务，或任意 PG16）
# 1. 建表（Alembic，启动代码不建表）
uv run --package mock-mes mock-mes-migrate upgrade head
# 2. 生成数据窗口（幂等；缺日补齐）
uv run --package mock-mes mock-mes-generate --fill-missing
# 3. 启动服务（默认 127.0.0.1:8010）
uv run --package mock-mes mock-mes
```

常用环境变量（前缀 `MOCK_MES_`）：`MOCK_MES_DATABASE_URL`（**必需**）、`MOCK_MES_HOST`、
`MOCK_MES_PORT`、`MOCK_MES_SCENARIO`（small/standard）、`MOCK_MES_SEED`、
`MOCK_MES_VIRTUAL_NOW`、`MOCK_MES_DATA_START`（默认去年 1 月 1 日）、`MOCK_MES_DATA_END`。

健康检查：`GET /health/live`、`GET /health/ready`（后者会校验数据库连接）。业务接口在
`/api/` 下，共 27 个，全部 POST + JSON。

## 关键文件

| 文件 | 作用 |
| --- | --- |
| `src/mock_mes/api/server.py` | 应用组装：`create_app()`、health（readiness 查库）、错误处理器、PG 池与 store 装配 |
| `src/mock_mes/api/customer.py` | **全部 27 个业务端点** + 公共辅助（认证、过滤、分页、信封） |
| `src/mock_mes/api/faults.py` | `X-Mock-Fault` 故障注入中间件 |
| `src/mock_mes/db.py` | `MockMesDb`：API 只读连接池（psycopg async） |
| `src/mock_mes/store.py` | `MockMesStore`：SQL 行级过滤、SQL 分页/合计、三源工资归一化 |
| `src/mock_mes/generator/fixtures.py` | 主数据 + Story-5/6/7 锚定 fixture（逐字保留） |
| `src/mock_mes/generator/engine.py` | 确定性生成引擎（纯函数 `compute_day_rows` + 写入/批次） |
| `src/mock_mes/generate.py` | `mock-mes-generate` CLI（手动/定时触发入口） |
| `src/mock_mes/migrations.py` | `mock-mes-migrate` CLI（独立版本表） |
| `src/mock_mes/identities.py` | `IDENTITIES`、`APP_KEY_TO_COMPANY`（原 seed.py 移出） |
| `migrations/` + `alembic.ini` | Alembic 迁移（版本表 `alembic_version_mock_mes`） |
| `src/mock_mes/testing.py` | 测试共享助手（测试库迁移/生成窗口/构造 app） |
| `tests/unit/test_generator.py` | 生成器纯函数测试：确定性、不变量、锚定、窗口边界（无需 DB） |
| `tests/integration/test_pg_generator.py` | PG 集成测试：迁移、幂等、批次 hash、SQL 过滤、PG 不可用报错 |
| `tests/golden/wages_v1.json` | 工资 golden 数据（Story 10 已随数据窗口重生成并记录原因） |

数据/接口形状的权威来源是 `contracts/mes-canonical.openapi.yaml`（在仓库根，不在本目录）。

## 生成器使用

```bash
# 全窗口补齐（默认从去年 1 月 1 日到今天/virtual_now）
uv run --package mock-mes mock-mes-generate --fill-missing
# 指定单天
uv run --package mock-mes mock-mes-generate --day 2026-08-28 --days 1
# 指定窗口
uv run --package mock-mes mock-mes-generate --start 2026-08-01 --end 2026-08-31
```

- 幂等：`(seed, day)` 已生成则跳过（批次表唯一键）。
- 批次表 `mock_generate_batch`：run_id、day、scenario('default')、seed、行数、数据 hash、状态；
  全窗口 hash 可作对拍基线。
- 工作日历：周末 + 内置节假日不排产（滚动产量只落在工作日）。

## 修改 / 新增 API 的流程

1. **先看数据是否够用**：锚定 fixture 在 `src/mock_mes/generator/fixtures.py`，滚动生成在
   `src/mock_mes/generator/engine.py`。注意保持既有业务不变量（`je = sl * price`，工资三源
   对账，扫码工序集自洽）。改生成逻辑影响 golden 时**必须在 Story 中记录原因**。
2. **写端点**：在 `src/mock_mes/api/customer.py` 中按既有模式添加：

   ```python
   @router.post("/api/NetYf/Baseinfo/MyNewQuery")
   async def my_new_query(request: Request) -> JSONResponse:
       body = await _json_body(request)
       check_common_params(body)  # 校验 app_key/timestamp/sign
       identity = identity_from(request)  # Bearer 解析身份
       require_same_tenant(identity, app_key)  # 需要时校验租户一致
       result = await store_from(request).page("mock_xxx", identity, ...)  # SQL 过滤+分页
       return JSONResponse(content=ok({"list": result.items, "total": result.total}))
   ```

   - 响应必须用 `{code, message, result, timestamp}` 信封：成功 `ok(...)`（code=1），
     失败抛 `MesError(status_code, "客户原文")` 或 `fail(...)`（code=0）。
   - 行级过滤与分页/合计**在 SQL 完成**（`store.page` / `store.sum_rows` /
     `store.distinct_count`），不把全表拉回内存。
   - 新增端点无需改 `server.py`（router 已注册）；新表要加迁移文件与 `_COLUMN_MAP`。
3. **同步 OpenAPI 契约**（如端点属于客户真实接口）：在
   `contracts/mes-canonical.openapi.yaml` 中登记 operation 与响应 schema，
   否则 `tests/contract/test_mock_mes_canonical.py` 校验会失败。
4. **补测试**：`tests/unit/test_generator.py`（纯函数确定性/不变量）与
   `tests/unit/test_customer_api.py`（接口行为）。改数据时不要悄悄改动既有 golden
   期望值。

## 测试

```bash
# 单测（生成器纯函数测试无需数据库；接口/golden 测试需要测试库）
MOCK_MES_TEST_DATABASE_URL=postgresql://mock_mes:mock_mes_dev@127.0.0.1:3432/mock_mes \
  uv run --no-sync pytest mock-mes/tests

# 契约测试（响应是否符合 OpenAPI schema）
MOCK_MES_TEST_DATABASE_URL=postgresql://mock_mes:mock_mes_dev@127.0.0.1:3432/mock_mes \
  uv run --no-sync pytest tests/contract/test_mock_mes_canonical.py

# 仓库级全套检查（lint + typecheck + 单测 + 契约 + 集成 + e2e + security）
make check
```

未设置 `MOCK_MES_TEST_DATABASE_URL` 时，需要 PG 的测试按惯例跳过（与 usage-admin 相同）；
CI 通过 postgres service 提供测试库。

测试身份（Bearer 固定 token，驱动行级过滤）：
`MOCK-TOKEN-01009`（厂长，全厂）、`MOCK-TOKEN-01008`（车间主任，本车间）、
`MOCK-TOKEN-01001`（员工，仅本人）、`MOCK-TOKEN-02001`（乙厂，隔离验证）。
AppKey：`APPKEY-A`（甲厂）/ `APPKEY-B`（乙厂）。

故障注入（单请求，不持久）：请求头 `X-Mock-Fault: latency|429|5xx|404|duplicate_page|
missing_page|wrong_total|footer_mismatch|null|field_drift`，`X-Mock-Latency-Ms` 上限 2000。

## 注意

- 公共参数 `sign` 是确定性占位值（`sign_of()`），**不要**实现客户真实签名算法。
- 不要 import `factory_agent`；兼容性只通过 OpenAPI 契约验证。
- 连接凭据只来自环境变量，**不得**出现在日志、快照、错误信息或测试输出中。
- API 进程只读、生成器独立进程写入；Compose 控制启动顺序（先迁移后启动）。
