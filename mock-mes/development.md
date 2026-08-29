# mock-mes 开发指南

模拟客户 MES 的确定性服务，用于本地开发/测试/演示，**不是生产依赖**。数据完全由
`(scenario, seed, virtual_now)` 决定，无数据库、无迁移，每次启动在内存中重建。

## 快速开始

在仓库根目录（或 mock-mes 目录）执行：

```bash
# 启动服务（默认 127.0.0.1:8010）
uv run --package mock-mes mock-mes          # 或 make dev-mock

# 只构建数据集并打印 hash（不启动服务）
uv run --package mock-mes mock-mes-seed --scenario small --seed 20260821
```

常用环境变量（前缀 `MOCK_MES_`）：`MOCK_MES_HOST`、`MOCK_MES_PORT`、
`MOCK_MES_SCENARIO`（small/standard）、`MOCK_MES_SEED`、`MOCK_MES_VIRTUAL_NOW`。

健康检查：`GET /health/live`、`GET /health/ready`。业务接口在 `/api/` 下，共 27 个，
全部 POST + JSON。

## 关键文件

| 文件 | 作用 |
| --- | --- |
| `src/mock_mes/api/server.py` | 应用组装：`create_app()`、health、错误处理器、注册 router |
| `src/mock_mes/api/customer.py` | **全部 27 个业务端点** + 公共辅助函数（认证、过滤、分页、信封） |
| `src/mock_mes/api/faults.py` | `X-Mock-Fault` 故障注入中间件 |
| `src/mock_mes/seed.py` | `Dataset` 定义、`build_dataset()`、`IDENTITIES`、`APP_KEY_TO_COMPANY` |
| `src/mock_mes/config.py` | `MockMesSettings` |
| `tests/unit/test_customer_api.py` | 端点单元测试（主要测试入口） |
| `tests/golden/wages_v1.json` | 工资 golden 数据 |
| `scripts/test_api.py` | 手动联调客户端（自动取 token + 带公共参数） |

数据/接口形状的权威来源是 `contracts/mes-canonical.openapi.yaml`（在仓库根，不在本目录）。

## 修改 / 新增 API 的流程

1. **先看数据是否够用**：`src/mock_mes/seed.py` 里的 `Dataset`（如 `plans`、`ysk`、
   `wsk`、`employees`…）。没有所需字段/数据就在 `build_dataset()` 里补，注意保持
   既有业务不变量（如 `je = sl * price`，工资三源对账，扫码工序集自洽）。
2. **写端点**：在 `src/mock_mes/api/customer.py` 中按既有模式添加：

   ```python
   @router.post("/api/NetYf/Baseinfo/MyNewQuery")
   async def my_new_query(request: Request) -> JSONResponse:
       body = await _json_body(request)
       check_common_params(body)  # 校验 app_key/timestamp/sign
       identity = identity_from(request)  # Bearer 解析身份
       require_same_tenant(identity, app_key)  # 需要时校验租户一致
       rows = visible_rows(dataset_from(request).xxx, identity)  # 行级过滤
       return JSONResponse(content=ok(paginate(rows, body)))  # 信封+分页
   ```

   - 响应必须用 `{code, message, result, timestamp}` 信封：成功 `ok(...)`（code=1），
     失败抛 `MesError(status_code, "客户原文")` 或 `fail(...)`（code=0）。
   - 列表接口返回 `result.total`，客户有的还带 `result.footer`。
   - 常见辅助：`paginate()`、`date_window()` / `in_date_window()`、`sum_of()`、`_json_body()`。
   - 新增端点后**无需**改 `server.py`（router 已注册），但新数据集字段要加进 `Dataset`。
3. **同步 OpenAPI 契约**（如端点属于客户真实接口）：在
   `contracts/mes-canonical.openapi.yaml` 中登记 operation 与响应 schema，
   否则 `tests/contract/test_mock_mes_canonical.py` 校验会失败。
4. **补测试**：在 `tests/unit/test_customer_api.py` 加用例（参考 `test_row_level_filtering_three_tiers`）。
   每个端点都要有模型/generator 单元测试 + 契约测试。改数据时不要悄悄改动既有 golden
   期望值。

## 测试

```bash
# 单测（全部 mock-mes 单测）
uv run --no-sync pytest mock-mes/tests/unit

# 契约测试（响应是否符合 OpenAPI schema）
uv run --no-sync pytest tests/contract/test_mock_mes_canonical.py \
    tests/contract/test_canonical_mes_openapi.py

# 仓库级全套检查（lint + typecheck + 单测 + 契约 + 集成 + e2e + security）
make check

# 只跑 lint / 类型检查
make lint
make typecheck
```

手动联调（服务已启动）：

```bash
# 方式一：用现成脚本
uv run python mock-mes/scripts/test_api.py

# 方式二：curl 走完整认证链
TOKEN=$(curl -s http://127.0.0.1:8010/api/system/token \
  -H 'Content-Type: application/json' -d '{"app_key":"APPKEY-A"}')
# 取出 result 里的 accessToken/appkey/timestamp/sign 后：
curl -s http://127.0.0.1:8010/api/NetYf/Baseinfo/UserInfoQuery \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ACCESS_TOKEN" \
  -d '{"app_key":"...","timestamp":...,"sign":"...","USERNAME":"admin"}'
```

测试身份（Bearer 固定 token，驱动行级过滤）：
`MOCK-TOKEN-01009`（厂长，全厂）、`MOCK-TOKEN-01008`（车间主任，本车间）、
`MOCK-TOKEN-01001`（员工，仅本人）、`MOCK-TOKEN-02001`（乙厂，隔离验证）。
AppKey：`APPKEY-A`（甲厂）/ `APPKEY-B`（乙厂）。

故障注入（单请求，不持久）：请求头 `X-Mock-Fault: latency|429|5xx|404|duplicate_page|
missing_page|wrong_total|footer_mismatch|null|field_drift`，`X-Mock-Latency-Ms` 上限 2000。

## 注意

- 公共参数 `sign` 是确定性占位值（`sign_of()`），**不要**实现客户真实签名算法。
- 不要 import `factory_agent`；兼容性只通过 OpenAPI 契约验证。
- `sign`、token 等只做形状模拟，不是安全原语；保持 `(scenario, seed, virtual_now)`
  确定性不变。
