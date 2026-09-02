# usage-admin 后台 API 文档（前端对接版）

- 版本：v1.0（2026-08-29，随 `.github/story/#9.md` 落地）
- 服务：`usage-admin`，多租户用量计量与运营管理服务
- 状态标识：✅ 已实现可用 ｜ ✅ 已实现（Story 9 交付；真实 MES 统计数据的端到端验证待 Story 11 计量数据产出后进行）
- 本文档是**前端对接的唯一接口依据**；字段语义、口径与权限的完整说明见
  [`docs/product/usage-admin-dashboard-gap.md`](../docs/product/usage-admin-dashboard-gap.md)。

## 1. 服务信息

| 项 | 值 |
|---|---|
| 服务名 | `usage-admin`（FastAPI） |
| 默认端口 | `8020`（`USAGE_ADMIN_PORT` 可配置） |
| 交互式文档 | `GET /docs`（Swagger UI）、`GET /openapi.json` |
| 健康检查 | `GET /health/live`、`GET /health/ready` |

## 2. 鉴权（必读）

usage-admin 自建平台运营账号体系，采用 **Bearer Token 认证**（D16）；同时保留「可信网关注入
三 header」作为开发 / 测试直连通道。**请求体 / URL 一律不得携带身份。**

### 2.1 认证方式

| 方式 | 说明 | 场景 |
|---|---|---|
| `Authorization: Bearer <token>` | **首选**。token 由登录接口签发，或由 `USAGE_ADMIN_API_TOKEN` 配置下发 | 前端调用、内部运营调用 |
| 三 header（网关注入） | `X-Platform-Principal` / `X-Platform-Role` / `X-Platform-Tenants` | 开发 / 测试直连（无 token 时生效） |

前端对接（D16）：使用平台下发的 `USAGE_ADMIN_API_TOKEN`，在请求头携带
`Authorization: Bearer <token>` 即可调用全部接口，**不需要实现注册 / 登录**。

内部运营：通过注册 / 登录接口获取 token 后调用。

### 2.2 角色

| 角色 | 读用量 | 看用户级明细 | 导出 | 工厂账户（读） | 工厂账户（写） | 运营账号管理 |
|---|---|---|---|---|---|---|
| `viewer` | ✅ | ✅ | ❌ | ✅ | ❌ | ❌ |
| `analyst` | ✅ | ✅ | ✅ | ✅ | ❌ | ❌ |
| `admin` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

越权行为返回 `403`。token 中携带 `principal_id` / `role` / 可看租户集合；租户集合为空表示
平台全部租户，非空则仅可访问列出的 AppKey（与 `X-Platform-Tenants` 语义一致）。

## 3. 通用约定

### 3.1 时间

- 所有查询接口必须带 `start` 与 `end`，ISO 8601 格式（推荐 UTC，`2026-08-01T00:00:00Z`），`start` 必须在 `end` 之前；
- 最大时间跨度 366 天；超过返回 `422`；
- 响应中的时间为 UTC，`timezone` 字段标注展示时区（默认 `Asia/Shanghai`）。

### 3.2 分页

- 涉及列表的接口统一使用 `limit` / `offset`；
- `limit` 默认见各接口说明，上限 `200`，超过会截断（不报错）。

### 3.3 错误

统一使用 HTTP 状态码 + `{"detail": "..."}`：

| 状态码 | 含义 |
|---|---|
| `400` | 请求不合法（仅个别接口） |
| `403` | 缺身份 header / 角色不足 / 租户越权 |
| `404` | 资源不存在（如导出记录、账户不存在） |
| `422` | 参数校验失败（时间不合法、跨度超限、维度不支持等） |

### 3.4 响应元字段

用量类响应统一携带以下字段，前端**不得把不完整结果当作完整结果展示**：

| 字段 | 类型 | 说明 |
|---|---|---|
| `metric_version` | string | 指标口径版本（`rollup=...;contract=...;p=...`），口径变更后字段格式不变但值变化 |
| `timezone` | string | 展示时区 |
| `incomplete` | bool | `true` 表示存在数据缺口（如 rollup 未覆盖某些时段），应提示「数据不完整」 |
| `freshness` | datetime\|null | 数据最新统计时间（仅 summary） |

### 3.5 AppKey 脱敏

- **AppKey 是客户 MES 凭证**。除「新增账户」的响应外，所有出参中的 AppKey 一律为
  **前 6 位 + `***`**（如 `fac-01***`），前端不得要求接口返回明文；
- 新增账户时明文 AppKey 仅返回一次，前端应提示运营人员保存；
- 「按 AppKey 筛选」（`X-Platform-Tenants` 或查询参数）仍传明文，由服务端匹配。

## 4. 接口总览

### 4.0 平台运营账号（内部使用，D15；前端不需要）

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| POST | `/admin/v1/auth/register` | 注册运营账号（仅 `admin`） | ✅ |
| POST | `/admin/v1/auth/login` | 登录，签发 Bearer token | ✅ |

> 前端对接**不需要**调用本组接口——使用平台下发的 `USAGE_ADMIN_API_TOKEN`（见 §2.1）。

### 4.1 工厂账户管理（Tab：工厂账户配置）

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/admin/v1/tenants/registry` | 账户列表（分页） | ✅ |
| GET | `/admin/v1/tenants/registry/{app_key}` | 单个账户详情 | ✅ |
| POST | `/admin/v1/tenants/registry` | 新增账户 | ✅ |
| PATCH | `/admin/v1/tenants/registry/{app_key}` | 编辑（名称 / 状态） | ✅ |
| DELETE | `/admin/v1/tenants/registry/{app_key}` | **停用**（非物理删除） | ✅ |
| POST | `/admin/v1/tenants/registry/{app_key}/enable` | 重新启用 | ✅ |

### 4.2 用量统计看板（Tab：用量统计看板）

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| GET | `/admin/v1/tenants` | 时间范围内有数据的 AppKey 列表 | ✅ |
| GET | `/admin/v1/usage/summary` | 总览：用户数、提问数、Token、耗时分位 | ✅ |
| GET | `/admin/v1/usage/timeseries` | 时间序列（小时 / 天） | ✅ |
| GET | `/admin/v1/usage/dimensions` | 维度分布 | ✅ |
| GET | `/admin/v1/usage/users` | 用户级活跃（分页） | ✅ |
| GET | `/admin/v1/usage/by-tenant` | 按工厂分组的用量明细 + 分页（F1.13 表格） | ✅ |
| GET | `/admin/v1/usage/mes-categories` | MES 调用成功数，按产量 / 工资 / 订单 / 其他四类 | ✅ |
| GET | `/admin/v1/usage/mes-failures` | MES 调用失败数，按分类 + 错误类别 | ✅ |
| GET | `/admin/v1/usage/mes-operations` | 按具体 operation_id 的调用明细 | ✅ |
| GET | `/admin/v1/usage/models` | 按实际模型统计调用与 Token | ✅ |
| GET | `/admin/v1/usage/capabilities` | 按智能体能力分布 | ✅ |
| GET | `/admin/v1/usage/errors` | 按错误类别分布 | ✅ |

### 4.3 导出

| 方法 | 路径 | 说明 | 状态 |
|---|---|---|---|
| POST | `/admin/v1/exports` | 创建导出（csv / xlsx） | ✅ |
| GET | `/admin/v1/exports/{export_id}` | 查询导出状态与下载地址 | ✅ |
| GET | `/admin/v1/exports/{export_id}/download` | 下载（带签名 token） | ✅ |

## 5. 接口明细

> 以下示例为约定响应结构；`start`/`end` 均省略。所有请求需带 §2 的三个 header。

### 5.1 工厂账户管理

#### 5.1.1 账户列表

```
GET /admin/v1/tenants/registry?limit=20&offset=0
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `limit` | int | 否 | 每页条数，默认 20，上限 200 |
| `offset` | int | 否 | 偏移，默认 0 |

响应 `200`：

```json
{
  "items": [
    {
      "app_key": "fac-01***",
      "tenant_name": "温州制衣一厂",
      "status": "active",
      "created_at": "2026-07-02T00:00:00Z",
      "updated_at": "2026-08-28T02:30:00Z"
    }
  ],
  "total": 3,
  "next_cursor": 20,
  "timezone": "Asia/Shanghai"
}
```

字段说明：`status` 取值 `active`（启用）/ `disabled`（停用）；`app_key` 已脱敏；`next_cursor`
为下一页 `offset`，为 `null` 表示无更多。

#### 5.1.2 账户详情

```
GET /admin/v1/tenants/registry/{app_key}
```

响应 `200`：同列表单项（含 `tenant_name`、`status`、`created_at`、`updated_at`）。
`404`：账户不存在。

#### 5.1.3 新增账户

```
POST /admin/v1/tenants/registry
```

请求体：

```json
{
  "tenant_name": "新工厂名称",
  "status": "active"
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `tenant_name` | string | 是 | 工厂 / 企业全称 |
| `status` | string | 否 | `active`（默认）/ `disabled` |

响应 `201`（**仅此接口返回明文 AppKey，须由前端提示保存**）：

```json
{
  "app_key": "fac-新生成的明文Key",
  "tenant_name": "新工厂名称",
  "status": "active",
  "created_at": "2026-08-29T10:00:00Z",
  "updated_at": "2026-08-29T10:00:00Z"
}
```

#### 5.1.4 编辑账户

```
PATCH /admin/v1/tenants/registry/{app_key}
```

请求体（均为可选，至少传一个）：

```json
{
  "tenant_name": "更名后的工厂",
  "status": "disabled"
}
```

响应 `200`：更新后的账户（AppKey 脱敏）。`404`：账户不存在。

#### 5.1.5 停用账户（F2.4 删除按钮）

```
DELETE /admin/v1/tenants/registry/{app_key}
```

- 语义为**停用**：`status` 置 `disabled`，**不做物理删除**，历史用量与事件全部保留；
- 停用后 factory-agent 不再为该租户发起新的 MES 调用；
- 响应 `204`。

#### 5.1.6 重新启用

```
POST /admin/v1/tenants/registry/{app_key}/enable
```

响应 `200`：更新后的账户（`status: "active"`）。`404`：账户不存在。

### 5.2 用量统计

#### 5.2.1 租户列表（✅ 现有）

```
GET /admin/v1/tenants?start=...&end=...
```

响应 `200`：`["fac-01***", "fac-02***", ...]`，为时间范围内有数据、且当前操作者获准查看的
AppKey 列表（已脱敏）。用于看板顶部「工厂」筛选下拉的初筛。

#### 5.2.2 总览 summary（✅ 现有）

```
GET /admin/v1/usage/summary?start=...&end=...
```

响应 `200`：

```json
{
  "tenant_ids": ["fac-01***"],
  "start": "2026-08-01T00:00:00Z",
  "end": "2026-08-29T00:00:00Z",
  "users": 128,
  "questions": 86240,
  "valid_questions": 86001,
  "status": {"completed": 85400, "failed": 500, "cancelled": 300, "rejected": 40},
  "llm_logical_calls": 172480,
  "llm_physical_attempts": 176120,
  "tokens": {"prompt_tokens": 842000, "completion_tokens": 426420, "cached_tokens": 0, "reasoning_tokens": 0},
  "durations": {
    "e2e_duration_ms": {"count": 86240, "mean_ms": 3420.5, "p50_ms": 2810.0, "p95_ms": 8900.0, "p99_ms": 15400.0},
    "mes_duration_ms": {"count": 86240, "mean_ms": 890.2, "p50_ms": 620.0, "p95_ms": 2200.0, "p99_ms": 4100.0},
    "llm_duration_ms": {"count": 86240, "mean_ms": 2100.8, "p50_ms": 1750.0, "p95_ms": 6100.0, "p99_ms": 9900.0},
    "local_duration_ms": {"count": 86240, "mean_ms": 120.1, "p50_ms": 80.0, "p95_ms": 400.0, "p99_ms": 900.0}
  },
  "metric_version": "rollup=rollup-v2;p=percentile-cont-v1",
  "timezone": "Asia/Shanghai",
  "freshness": "2026-08-29T02:00:00Z",
  "incomplete": false
}
```

前端对照：F1.6 总 Token 消耗 = `prompt_tokens + completion_tokens + cached_tokens + reasoning_tokens`；
F1.7 总查询次数 = `questions`；耗时可看 `durations` 的 `mean_ms` / `p50_ms` / `p95_ms` / `p99_ms`。

#### 5.2.3 时间序列（✅ 现有，F1.11 每日 Token 趋势）

```
GET /admin/v1/usage/timeseries?start=...&end=...&granularity=day&metrics=prompt_tokens,completion_tokens
```

| 参数 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `granularity` | string | 否 | `hour` / `day`，默认 `day` |
| `metrics` | string | 否 | 逗号分隔指标名，默认 `users,questions,valid_questions` |

可用指标：`users`、`questions`、`valid_questions`、`status.completed`、`status.failed`、
`status.cancelled`、`status.rejected`、`llm_logical_calls`、`llm_physical_attempts`、
`prompt_tokens`、`completion_tokens`、`cached_tokens`、`reasoning_tokens`。

响应 `200`：

```json
{
  "tenant_ids": ["fac-01***"],
  "start": "2026-08-01T00:00:00Z",
  "end": "2026-08-29T00:00:00Z",
  "granularity": "day",
  "points": [
    {"bucket": "2026-08-01T00:00:00Z", "metrics": {"prompt_tokens": 29000.0, "completion_tokens": 14800.0}},
    {"bucket": "2026-08-02T00:00:00Z", "metrics": {"prompt_tokens": 31000.0, "completion_tokens": 15200.0}}
  ],
  "metric_version": "...",
  "timezone": "Asia/Shanghai",
  "incomplete": false
}
```

#### 5.2.4 维度分布（✅ 现有）

```
GET /admin/v1/usage/dimensions?start=...&end=...&dimension=capability
```

`dimension` 可选：`capability`、`status`、`entrypoint`、`role_category`、`error_category`、
`model_alias`、`actual_model`、`stage`、`fallback_reason`；不支持的值返回 `422`。

响应 `200`：

```json
{
  "tenant_ids": ["fac-01***"],
  "start": "...", "end": "...",
  "dimension": "capability",
  "values": {"fr001_personal_output": 31200.0, "fr002_personal_wage": 28600.0, "fr009_factory_overview": 6400.0},
  "truncated": false,
  "metric_version": "...",
  "timezone": "Asia/Shanghai"
}
```

> 注意：`capability` 是「智能体能力」口径，与 5.2.6 的 MES API 分类口径**不同**，前端
> 不得混用（详见产品文档 §2.2 陷阱说明）。

#### 5.2.5 用户级活跃（✅ 现有）

```
GET /admin/v1/usage/users?start=...&end=...&limit=50&offset=0
```

响应 `200`：

```json
{
  "tenant_ids": ["fac-01***"],
  "start": "...", "end": "...",
  "items": [{"user_subject_id": "e3f2...", "question_count": 421}],
  "total": 128,
  "next_cursor": 50,
  "metric_version": "...",
  "timezone": "Asia/Shanghai"
}
```

`user_subject_id` 为伪名化 ID（HMAC），不可反查真实员工。

#### 5.2.6 MES 调用成功数，按四类（✅，F1.8~F1.10、F1.12 饼图）

```
GET /admin/v1/usage/mes-categories?start=...&end=...
```

可选：`app_key` 参数（按单厂筛选）或依赖 `X-Platform-Tenants`。

响应 `200`：

```json
{
  "tenant_ids": ["fac-01***"],
  "start": "...", "end": "...",
  "categories": {"output": 32100, "payroll": 28600, "order": 25540, "other": 0},
  "total": 86240,
  "metric_version": "...",
  "timezone": "Asia/Shanghai",
  "incomplete": false
}
```

口径：`output` 产量查询、`payroll` 工资查询、`order` 订单进度、`other` 其他（认证 / 基础数据 /
吊挂）。四类之和 = 成功 MES 调用总数。饼图用 `categories` 四块绘制。

#### 5.2.7 MES 调用失败数（✅，D7 独立口径）

```
GET /admin/v1/usage/mes-failures?start=...&end=...
```

响应 `200`：

```json
{
  "tenant_ids": ["fac-01***"],
  "start": "...", "end": "...",
  "categories": {"output": 12, "payroll": 3, "order": 5, "other": 2},
  "by_error": {"mes_timeout": 8, "mes_5xx": 6, "mes_404": 4, "unauthorized": 4},
  "total": 22,
  "metric_version": "...",
  "timezone": "Asia/Shanghai"
}
```

#### 5.2.8 按工厂分组的用量明细（✅，F1.13 表格 + F1.14 分页）

```
GET /admin/v1/usage/by-tenant?start=...&end=...&limit=20&offset=0
```

| 参数 | 说明 |
|---|---|
| `name` | 可选，工厂名称模糊查询（F1.2） |
| `app_key` | 可选，按 AppKey 精确筛选（F1.3） |
| `limit` / `offset` | 分页，limit 默认 20，上限 200 |

响应 `200`：

```json
{
  "tenant_ids": ["fac-01***"],
  "start": "...", "end": "...",
  "items": [
    {
      "app_key": "fac-01***",
      "tenant_name": "温州制衣一厂",
      "status": "active",
      "token_total": 426200,
      "question_count": 26200,
      "mes_output": 9600,
      "mes_payroll": 8800,
      "mes_order": 7800,
      "mes_other": 0,
      "last_usage_at": "2026-08-28T02:30:00Z"
    }
  ],
  "total": 3,
  "next_cursor": 20,
  "metric_version": "...",
  "timezone": "Asia/Shanghai"
}
```

「最后统计时间」列用 `last_usage_at`；「状态」列用 `status`（账户状态，D8）。

#### 5.2.9 按 operation 明细（✅）

```
GET /admin/v1/usage/mes-operations?start=...&end=...
```

响应 `200`：`{"values": {"GongziMxQuery": 28600, "PlanGridPageList": 25540, ...}, "truncated": false, ...}`

#### 5.2.10 按模型统计（✅）

```
GET /admin/v1/usage/models?start=...&end=...
```

响应 `200`：`{"values": {"deepseek-v3": {"calls": 172480, "prompt_tokens": 842000, "completion_tokens": 426420}}, ...}`

#### 5.2.11 按能力分布（✅）

```
GET /admin/v1/usage/capabilities?start=...&end=...
```

响应 `200`：同 5.2.4 `dimensions=capability` 的结构，独立封装便于前端。

#### 5.2.12 按错误类别（✅）

```
GET /admin/v1/usage/errors?start=...&end=...
```

响应 `200`：`{"values": {"llm_timeout": 80, "llm_5xx": 40, "mes_timeout": 8, ...}, "truncated": false, ...}`

### 5.3 导出

#### 5.3.1 创建导出

```
POST /admin/v1/exports
```

请求体：

```json
{
  "start": "2026-08-01T00:00:00Z",
  "end": "2026-08-29T00:00:00Z",
  "format": "xlsx",
  "granularity": "day",
  "metrics": ["questions", "prompt_tokens", "completion_tokens", "mes_output", "mes_payroll", "mes_order", "mes_other"]
}
```

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `start` / `end` | datetime | 是 | 时间范围 |
| `format` | string | 否 | `csv` / `xlsx`，默认 `csv` |
| `granularity` | string | 否 | `hour` / `day`，默认按日 |
| `metrics` | string[] | 否 | 导出指标，支持新增 MES 分类指标 |

响应 `201`：

```json
{
  "export_id": "exp_xxx",
  "format": "xlsx",
  "status": "pending",
  "download_url": null,
  "expires_at": null,
  "created_at": "2026-08-29T10:00:00Z"
}
```

#### 5.3.2 查询导出状态

```
GET /admin/v1/exports/{export_id}
```

响应 `200`：`download_url` 就绪后非空（短时签名链接）。

#### 5.3.3 下载

```
GET /admin/v1/exports/{export_id}/download?token=...
```

`token` 为 5.3.2 返回的 `download_url` 中携带的签名参数；`Content-Disposition` 为附件下载。
`403`：链接失效或已过期。

## 6. 前端功能点 ↔ 接口对照

| 看板功能 | 接口 |
|---|---|
| F1.1 日期范围筛选 | 所有查询接口的 `start` / `end` |
| F1.2 按工厂名称 / ID 筛选 | `by-tenant?name=`（名称）/ `tenants`（下拉初筛） |
| F1.3 按 API-Key 筛选 | `by-tenant?app_key=` |
| F1.5 导出报表 | `POST /exports` + `GET /exports/{id}` + `download` |
| F1.6 总 Token 消耗 | `summary.tokens` 四项求和 |
| F1.7 智能体总查询次数 | `summary.questions` |
| F1.8~F1.10 三类接口调用 | `mes-categories.output / payroll / order` |
| F1.11 每日 Token 趋势 | `timeseries?granularity=day&metrics=prompt_tokens,...` |
| F1.12 接口类型分布饼图 | `mes-categories.categories`（四块） |
| F1.13 工厂明细表格 | `by-tenant` |
| F1.14 分页 | `by-tenant?limit&offset` |
| F2.1~F2.6 工厂账户配置 | `tenants/registry` 六个接口 |
