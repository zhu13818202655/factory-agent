# AI 助手前端对接 API 文档

本文档面向工厂 MES 智能问答助手的前端接入（PC 端悬浮小助手 + App 端双入口），描述
`factory-agent` 对前端提供的全部 HTTP 接口：当前身份（角色与数据范围）、会话管理（列表 /
新建 / 详情）、问答与 SSE 流式返回、历史消息、快捷问题、历史/收藏/一键复问、导出下载与
健康检查。

文档分界：本文档只描述智能体对前端的 API。智能体调用客户 MES 的接口契约见
`docs/product/AI问答对外接口-整理.md`；平台统计与租户管理相关接口见
`docs/api/统计与运营接口.md`（同进程、`/v1/statistics` 前缀、Bearer 鉴权）。前端不直接调用
后两者。

## 1. 基本约定

- API 前缀：业务接口统一为 `/v1`；健康检查为 `/health/*`。本文只写路径，不写域名与端口。
- 认证方式：所有业务接口都必须在请求头携带 `X-Factory-Credential`，值为**加密的
  app_key**。服务端以该凭据在客户 MES 的 `/api/system/token` 换取凭证包
  （`accessToken` / `appkey` / `sign` / `timestamp` 与 `user` / `uname` / `dept` /
  `roles`），后续业务取数由服务端携带 `Authorization: Bearer {accessToken}` 与请求体三参
  完成。**前端全程不接触、不保存 `accessToken`**。
- 身份唯一来源：租户、用户、角色与数据范围只来自上述 token 换取结果。**请求体与查询参数中
  绝不含租户、用户或范围字段**；即使误传 `tenant_id` / `user_id` 等字段也会被忽略，以凭据
  解析出的身份为准。
- 降级模式：仅当服务端未配置 token 网关（测试/联调降级环境）时，身份改用请求头
  `X-Factory-Tenant-Id` 与 `X-Factory-User-Id`。生产环境恒走
  `X-Factory-Credential`，前端按生产形态实现即可。
- 健康检查接口不需要凭据。
- 角色：token 返回的 `roles` 为权威角色码，共四档：`00` 员工 / `01` 组长 / `02` 管理 /
  `99` 老板。角色决定可用能力与快捷问题（见附录 A）。前端用 `GET /v1/me`（§4.16）取得
  当前角色与数据范围，**不要**自行推断、本地存储或声明角色。
- 统一错误响应：HTTP 错误一律为 `{"detail": "..."}`（请求体 schema 校验失败时为 FastAPI
  标准 `{"detail": [...]}` 结构）。业务过程中的权限不足、追问、失败**不是 HTTP 错误**，
  而是 SSE 事件流内的 `interaction.failed` / `interaction.clarification` 事件 +
  持久化消息，见第 5 节。
- 时间：服务端时区 `Asia/Shanghai`；时间字段为 ISO 8601 字符串。
- 分页：列表接口使用 `limit` + `cursor` 查询参数，响应携带 `next_cursor`（为 `null`
  表示没有下一页）。
- 排障：可选携带 `X-Request-ID` 请求头，服务端会原样回传该头，便于与后端日志对账。

## 2. 推荐前端流程

1. 打开助手面板时，调用 `GET /v1/me`（§4.16）取得当前身份：角色（`role_code`）与数据范围
   （`data_scope`），据此渲染角色化 UI；随后调用 `GET /v1/quick-questions` 拉取当前角色的
   快捷问题按钮；可选调用 `GET /v1/favorites` 展示收藏入口。
2. 用户提问（或点击快捷问题）时，调用
   `POST /v1/sessions/{session_id}/interactions`，请求体为 `text`（卡片下钻时另带可选
   `drill`，见 §4.2），返回 201 与 `interaction_id`。`session_id` 建议先由
   `POST /v1/conversations` 创建取得（§4.14），也可由前端自行生成后在首轮建档（见 §3.1）。
3. 用返回的 `interaction_id` 调用 `GET /v1/interactions/{interaction_id}/stream`
   订阅 SSE 事件流，按 §5 渲染阶段、追问与结果。
4. 收到终态事件（`interaction.completed` / `interaction.failed` /
   `interaction.cancelled`）后结束本轮加载态。
5. 收到 `interaction.result` 且 `artifact_id` 非空时，展示"导出报表"入口；用户点击后调用
   `GET /v1/artifacts/{artifact_id}/download`，把响应作为文件直接下载/保存（落盘保留，
   保留期内可重复下载）。
6. SSE 意外断开时，用 `Last-Event-ID` 重连断点续传（§5.1），**不要**重新发起提问。
7. 刷新页面 / 重新打开面板后，用 `GET /v1/conversations` 取回自己的历史会话列表，
   再用 `GET /v1/conversations/{session_id}` 恢复该会话的完整会话流（§4.13 / §4.15）；
   已知 `session_id` 时也可继续用 `GET /v1/sessions/{session_id}/messages`（§4.5）。
   跨会话的历史**查询意图**入口用 `GET /v1/history`（与会话列表不是同一概念）。
8. 收藏的一键复问：`POST /v1/favorites/{favorite_id}/re-ask` 取回保存的查询意图，由前端
   转成自然语言问题后回到第 2 步重新执行。

## 3. 核心对象

### 3.1 Session

一次多轮对话，服务端按 `(租户, 用户)` 归属记录其下的 interaction 与消息。

会话已成为**一等实体**：会话列表 / 新建会话 / 会话详情由 §4.13–§4.15 提供
（面向前端的完整交付文档见 `docs/api/会话接口-前端对接.md`）。
`session_id` 有两种取得方式：① 由 `POST /v1/conversations` 创建并返回（**推荐**）；
② 老客户端仍可自行生成（非空字符串即可，建议 UUID）并在首轮提问时由服务端自动建档——
因此**未改造的老前端不需要任何改动，其会话同样会出现在会话列表里**。

- 会话上下文恢复路径：`GET /v1/conversations`（列出自己的历史会话）→
  `GET /v1/conversations/{session_id}`（取回该会话的完整会话流）；已知 `session_id` 时
  也可继续用 `GET /v1/sessions/{session_id}/messages`（§4.5）。
- 前端**不再需要**把 `session_id` 存本地才能恢复历史；本地存储仅可用于记住
  「上次打开的是哪个会话」这类 UI 记忆。
- `GET /v1/history`（§4.7）是按用户维度的**查询意图**历史，不含会话维度与原始问句，
  与会话列表不是同一概念，两者不能互相替代。

### 3.2 Interaction

一次用户提问对应的一轮后端处理。接口返回的视图为：

```json
{
  "interaction_id": "it_xxx",
  "session_id": "sess_xxx",
  "status": "pending",
  "state": "parsing"
}
```

`status`（本轮处理状态）：

| 值 | 含义 |
| --- | --- |
| `pending` | 已建档，尚未开始执行 |
| `running` | 正在执行 |
| `completed` | 已完成（含正常结果与追问两种正常收尾） |
| `failed` | 失败（含权限不足的友好拒绝） |
| `cancelled` | 用户取消 |

`state`（会话状态机当前状态）：

`parsing`（解析）→ `clarifying`（追问）/ `authorizing`（鉴权）→ `executing`（取数）→
`composing`（计算）→ `answered`（已回答）；终态另有 `cancelled`、`failed`、`archived`。
前端可将 `state` 与阶段文案的映射用于进度展示：阶段**变化**以 `interaction.phase` 为准，
阶段**进行中**以 `interaction.progress` 为准（§5.2），思考过程以 `interaction.thinking`
为准（§5.2）。

### 3.3 Message

持久化消息，`GET /v1/sessions/{session_id}/messages` 返回的视图为：

```json
{
  "message_id": "msg_xxx",
  "interaction_id": "it_xxx",
  "role": "assistant",
  "kind": "result_table",
  "sequence": 6,
  "text": "已返回 12 行结果。",
  "created_at": "2026-09-21T02:15:33+00:00",
  "payload": {
    "capability_id": "fr001_personal_output",
    "columns": ["rq", "huohao", "worktype", "output_qty"],
    "column_titles": {"rq": "日期", "huohao": "款号", "worktype": "工序", "output_qty": "产量"},
    "row_count": 12,
    "incomplete": false,
    "incomplete_reason": null,
    "artifact_id": "art_xxx",
    "time_range": { "start": "2026-09-14T16:00:00+00:00", "end": "2026-09-18T16:00:00+00:00", "label": "2026-09-15 至 2026-09-18" },
    "consistency": null
  }
}
```

`role`：`user` / `assistant` / `system`

`kind`：

| 值 | 含义与建议渲染 |
| --- | --- |
| `plain_text` | 普通文本（用户提问） | 普通气泡 |
| `chat` | 闲聊应答文本（问候/闲聊/常识问答等非业务轮，`role=assistant`，`text` 为可直接展示的回复） | 普通气泡，按 assistant 展示 |
| `clarification` | 助手追问（缺时间、款号等条件时） | 追问气泡，引导用户补充 |
| `phase` | 阶段变化记录 | 可折叠的过程信息 |
| `thinking` | 本轮推理原文（思考过程）**全文**，`role=assistant`，`payload.elapsed_ms` 为本轮累计思考耗时 | 折叠块「思考过程 · Ns」，默认收起，展开显示 `text` |
| `result_table` | 结果卡片消息，`text` 为结果摘要（如"已返回 N 行结果。"） | 结果卡片 |
| `error` | 错误/拒绝/取消提示，`text` 为可直接展示的友好文案 | 错误提示气泡 |

`sequence` 为该消息在轮内的单调递增序号，与 SSE 的 `id:` 同源。
**它每轮都从 1 重新计数，只表示轮内先后，不能当整个会话的排序键**；
消息列表接口（§4.5 / §4.15）已按 `(created_at, interaction_id, sequence)` 升序返回，
前端直接按数组顺序渲染即可。

`interaction_id` 为该消息所属**轮次**，与 `interactions[]` / SSE 的 `interaction_id` 一一对应；
`created_at` 为消息创建时间。两者是 2026-09-21 的加法式新增字段（旧前端忽略即可），
拿到它们才能把消息归回「轮」并按轮展示收尾状态与时间戳（§4.5 / §4.15）。

`payload`（可选，仅 `result_table` 消息返回）：结果卡片元数据，与 `interaction.result`
事件的字段一致（`capability_id` / `columns` / `row_count` / `incomplete` /
`incomplete_reason` / `artifact_id` / `consistency` / `time_range`，并同样携带可选的 `card`
结构化卡片，见 §5.3）。前端可用它**重建结果卡片**：渲染卡片/KPI/预览行、列名、
行数与完整性标记，并凭 `artifact_id` 提供导出入口；消息无 payload（如早期
版本写入的记录）时该字段省略，前端回退为仅展示 `text` 摘要。SSE 事件不含 `payload`
字段，两者互不影响。**`payload.card` 与该轮 `interaction.result` 的 `data.card`
是同一份数据**，实时、断线重放与历史消息三路渲染结果完全一致；`payload.time_range`
同样如此，因此**刷新页面后从历史点击下钻依然能拿到原窗口**（§4.2）。

### 3.4 Artifact（导出产物）

结果导出采用**即时生成、落盘保留**策略：导出文件在内存中即时渲染后写入服务端产物存储
（S3 兼容对象存储；未配置对象存储时回退本地产物目录），**保留期默认 90 天、过期自动清理**，
**服务重启后在保留期内仍可下载**。`interaction.result` 携带的 `artifact_id` 是一次导出的下载凭证：

```json
{ "artifact_id": "art_xxx" }
```

前端拿到 `artifact_id` 后调用 `GET /v1/artifacts/{artifact_id}/download`，响应即文件
本体（`Content-Disposition: attachment`），浏览器直接下载、App 端保存到本地；不再需要
预签名链接。文件过期、被清理或 `artifact_id` 不属于当前用户（返回 404）时，引导用户
用历史/收藏一键复问重新生成。

## 4. 接口清单

以下所有业务接口都要求携带 `X-Factory-Credential` 请求头（§1），示例中不再重复。

### 4.1 健康检查

`GET /health/live`

```json
{ "status": "ok", "service": "factory-agent", "version": "0.1.0" }
```

`GET /health/ready`

```json
{
  "status": "ok",
  "service": "factory-agent",
  "version": "0.1.0",
  "dependencies": { "model": "fake" }
}
```

- `status`：`ok` / `degraded`；任一依赖为 `not_configured` 时 ready 返回 `degraded`。
- 前端探活只用 `/health/live`；`/health/ready` 供部署侧使用。

### 4.2 发起一轮问答

`POST /v1/sessions/{session_id}/interactions`

请求：

```json
{ "text": "我这个月的工资是多少？" }
```

- `text`：1–4000 字符，服务端当前有效上限默认 2000 字符，超出返回 400。
- 请求体除 `text` 外仅有一个可选字段 `drill`（结构化下钻，见下）；**不要携带任何身份、租户、范围字段**。
- `session_id` 由前端生成，首轮即视为建档该会话。

**可选字段 `drill`（结构化下钻，2026-09-16 拍板；时间窗口于 2026-09-20 补齐，D-7）**：
用户点击结果卡片上的下钻入口（`card.actions`，§5.3.1）时，前端把被点行的列值填入槽位，
并把**该卡回显的时间窗口**原样带上，发起本轮请求：

```json
{
  "text": "查看该车间工资",
  "drill": {
    "capability_id": "fr008_payroll_ranking",
    "dept_ids": ["001"],
    "time_range_start": "2026-09-14T16:00:00+00:00",
    "time_range_end": "2026-09-18T16:00:00+00:00"
  }
}
```

| 字段 | 说明 |
| --- | --- |
| `drill.capability_id` | 必填，下钻目标能力（与 `card.actions[].capability_id` 一致）；未登记的能力返回 400 |
| `drill.dept_ids` | 可选，目标车间/小组编号数组（来自被点行的 `bind_from` 映射列）；最多 20 个 |
| `drill.employee_uid` | 可选，目标员工工号（成员工资条下钻，fr012） |
| `drill.order_codes` / `style_codes` / `plan_codes` / `material_ids` | 可选业务筛选数组，每项 1–128 字符、每类最多 20 项；从服务端模板或已审核 `bind_from` 取真实标识，不能使用显示标签。`order_codes` 在计划汇总能力中指计划号，在包明细能力中指制单号。`material_ids` 可用于进度能力的默认明细先筛选再聚合；FR6 及任何工资来源能力不支持物料筛选，发送非空 `material_ids` 在开始交互前返回 400，不静默忽略 |
| `drill.order_view` | 可选组内明细选择，字段与校验见 §5.3.2；存在时必须同时提供绝对 `time_range_start/end`。同能力展开、状态筛选、组内翻页都发起新的 interaction |
| `drill.group_page` / `group_page_size` / `data_snapshot_id` | 订单组列表分页，默认 1 / 50；每页最多 50 组。第 2 页起必须回传版本摘要和绝对时间；只用于已登记组内明细的能力，不与具体 `order_view` 混用，见 §5.3.2 |
| `drill.time_range_start` / `drill.time_range_end` | **可选，但必须成对**：值原样取自上一轮结果的 `card.time_range.start` / `card.time_range.end`（§5.3.1），ISO 8601 带时区（UTC 半开区间，`end` 为排他上界）。只给一半返回 **422**（客户端缺陷，服务端不猜边界）。**强烈建议前端总是回传**，否则下钻可能落到另一个窗口 |
| `drill.time_expression` | 可选，时间范围表述（如 `本月`）；**当 `time_range_start/end` 同时存在时它只是展示标签**，不参与取数窗口决策 |

**时间窗口择优顺序（服务端）**：`time_range_start/end` 回显窗口 → `time_expression` →
**同会话最近一次结果的窗口**（用户没提时间时自动继承，如「那后道车间呢？」）→ 当月兜底。
回传窗口同样过取数红线（单窗上限 / 不晚于明天 / 起止非倒置），违规以
`interaction.failed`（`error_category = drill_invalid_time`）终结，**零业务调用**。

服务端在下钻轮次上**跳过 LLM 意图路由**（确定性执行、零模型开销），并对槽位做三道校验：
能力在角色矩阵内、槽位键白名单、值只收窄（部门/员工必须在调用者可查范围内）。越界时本轮
以 `interaction.failed` 终结（`filter_forbidden` / `filter_not_found`），**零业务调用**。
`drill` 缺席时行为与旧版完全一致。

响应 `201`：

```json
{
  "interaction_id": "it_xxx",
  "session_id": "sess_xxx",
  "status": "pending",
  "state": "parsing"
}
```

错误：`400`（文本为空/超长、`capability_id` 未登记）、`422`（请求体结构校验失败，如 `drill`
只给了 `time_range_start` 而未给 `time_range_end`）、`401`（凭据缺失/无效）、`403`（身份解析拒绝）、
`502`（token 网关不可用）、`503`（会话服务未配置）。

### 4.3 订阅事件流（SSE）

`GET /v1/interactions/{interaction_id}/stream`

请求头（均可选）：

| 头 | 说明 |
| --- | --- |
| `Last-Event-ID` | 断点续传游标：只重放该序号之后的事件；缺失或非法时从头重放 |

响应类型 `text/event-stream`，响应头：

```http
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

事件帧格式与事件定义见第 5 节。注意：interaction 不存在或不属于当前身份时，返回
**空事件流**（不报 404），前端收不到任何事件时按失败处理并可回退到历史消息接口。

### 4.4 取消当前轮

`POST /v1/interactions/{interaction_id}/cancel`

无请求体。响应为 InteractionView（同 §3.2），`status` 为 `cancelled`（对已终态的轮幂等，
原样返回当前状态）。

- 只有用户明确点击"停止"时才调用；页面关闭、路由切换、SSE 断开**不应**触发取消。
- 取消成功后该轮会追加一条 `role=system`、`kind=error` 的消息："已取消本次查询。"

### 4.5 历史消息

`GET /v1/sessions/{session_id}/messages`

Query：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `limit` | 否 | 默认 50，上限 200 |
| `cursor` | 否 | 翻页游标，取上一页返回的 `next_cursor` |

响应：

```json
{
  "items": [
    {
      "message_id": "msg_1",
      "interaction_id": "it_1",
      "role": "user",
      "kind": "plain_text",
      "sequence": 1,
      "text": "我这个月的工资是多少？",
      "created_at": "2026-09-21T02:15:31+00:00"
    },
    {
      "message_id": "msg_2",
      "interaction_id": "it_1",
      "role": "assistant",
      "kind": "result_table",
      "sequence": 6,
      "text": "已返回 1 行结果。",
      "created_at": "2026-09-21T02:15:33+00:00"
    }
  ],
  "next_cursor": null
}
```

- 消息按 `(interaction, sequence)` 归属过滤：**只能看到当前身份自己的会话消息**。
  已认证但**非该会话所有者**的用户拿到的是 `200` + 空列表（`{"items": []}`），
  既不报错、也不暴露该会话是否存在；只有**凭据本身被拒**（非本厂成员、租户被停用等）才返回
  `403`（§6.1）。
- 消息按 `(created_at, interaction_id, sequence)` **升序**返回（自然时间顺序）；
  数组已排好序，前端**直接按数组顺序渲染，不要自行重排**（`sequence` 每轮从 1
  重新计数，不能当全局排序键，见 §3.3）。
- 消息新增 `interaction_id`（所属轮次）与 `created_at`（创建时间）两个字段
  （加法式变更，旧前端可忽略，定义见 §3.3）。
- 本接口的响应用 `exclude_none` 序列化：`next_cursor` 为 `null` 时**该键不存在**
  （会话接口 §4.13 / §4.15 则始终返回该键）。
- 本接口**保持可用**，但新前端**推荐改用 §4.15 的会话详情**：它是本接口的超集，
  另含会话元信息与轮次列表，且响应形状固定。

### 4.6 角色化快捷问题

`GET /v1/quick-questions`

响应：`QuickQuestion[]`，**按当前角色动态返回**（员工 3 条，组长/管理/老板各 4 条）：

```json
[
  {
    "id": "qq-own-wage",
    "capability_id": "FR-002",
    "text": "我这个月的工资汇总",
    "slots": { "time_expression": "本月" }
  }
]
```

- 点击快捷问题 = 用 `text` 字段内容发起一轮问答（§4.2）。
- 角色与快捷问题的对应关系见附录 A。

### 4.7 查询历史

`GET /v1/history`

Query：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `limit` | 否 | 默认 20，范围 1–100 |
| `cursor` | 否 | 翻页游标 |

响应：

```json
{
  "items": [
    {
      "history_id": "his_xxx",
      "capability_id": "FR-002",
      "intent": { "time_expression": "本月" },
      "status": "completed",
      "created_at": "2026-08-24T06:00:00+00:00"
    }
  ],
  "next_cursor": null
}
```

- 历史记录只保存归一化的查询意图（能力 + 非敏感槽位），不保存原始问句与数据值。
- `intent` 槽位可能包含：`time_expression` / `order_codes` / `plan_codes` /
  `style_codes` / `dept_names` / `employee_names`。

`DELETE /v1/history/{history_id}`

成功返回 `204`；不存在或不属于当前身份返回 `404`。

### 4.8 收藏与一键复问

`POST /v1/favorites`

请求：

```json
{
  "capability_id": "FR-002",
  "title": "我的本月工资汇总",
  "slots": { "time_expression": "本月" }
}
```

- `capability_id`：1–40 字符；`title`：1–200 字符；`slots` 只保留非敏感槽位白名单
  （同 §4.7 的 `intent` 字段），其余字段会被服务端剔除。

响应 `201`：

```json
{
  "favorite_id": "fav_xxx",
  "capability_id": "FR-002",
  "title": "我的本月工资汇总",
  "slots": { "time_expression": "本月" },
  "created_at": "2026-08-24T06:00:00+00:00",
  "expires_at": "2026-11-22T06:00:00+00:00"
}
```

- 收藏默认 90 天过期（`expires_at`），过期后复问返回 404。

`GET /v1/favorites`

Query：`limit`（默认 50，上限 200）。响应：`Favorite[]`（结构同上）。

`DELETE /v1/favorites/{favorite_id}`

成功返回 `204`；不存在或不属于当前身份返回 `404`。

`POST /v1/favorites/{favorite_id}/re-ask`

无请求体。响应：该收藏的 `Favorite` 视图。

- 复问语义：服务端返回保存的查询意图，**不回放任何缓存结果**；前端将其转成自然语言
  问题（通常即 `title` 文本）后走 §4.2 / §4.3 重新执行，重新取当前数据。
- 收藏不存在、不属于当前身份或已过期返回 `404`。

### 4.9 用户映射

`POST /v1/users/me/mapping`

请求：

```json
{ "uname": "张三", "company": "弘兆服饰" }
```

- `uname`：1–200 字符；`company`：可选，最长 200 字符。
- `uid` 来自凭据解析结果，不由前端提供。

响应：

```json
{ "uid": "10086", "uname": "张三", "company": "弘兆服饰" }
```

用途：保存 `uid ↔ uname/company` 的展示映射，供界面显示当前用户名称。

- 当前身份（角色 / 姓名 / 绑定部门 / 数据范围）由 `GET /v1/me`（§4.16）**权威**返回。
  本接口只保存**展示用的补充资料**（如公司名），`uname` **不作为身份来源**：`GET /v1/me`
  的 `uname` 恒取 token 返回值。
- 本接口是 `POST` 写操作，需显式调用；前端**不需要**为了拿到角色而调用它。

### 4.10 导出下载

`GET /v1/artifacts/{artifact_id}/download`

响应即文件本体（流式返回，`Content-Disposition: attachment`），前端/浏览器直接把响应当
文件下载；App 端保存到本地。文件名按"角色_功能_时间范围_生成时间"约定。

- `artifact_id` 来自 `interaction.result` 事件（§5.3），是一次导出的下载凭证。
- 导出**即时生成、落盘保留**：XLSX 在内存渲染后写入服务端产物存储（S3 兼容对象存储或
  本地产物目录），保留期默认 90 天（过期自动清理），**服务重启后在保留期内仍可下载**；
  不签发预签名链接。
- 未配置对象存储时导出与下载回退本地产物目录；配置了对象存储则写入该存储，两者对前端
  接口形态一致。文件过期、被清理或不可得时接口返回 404，应引导用户用历史/收藏
  一键复问**重新执行 → 重新下载**。
- 下载会重新校验凭据与归属：他人 `artifact_id`、已过期的与不存在的 `artifact_id` 一律
  返回 404（刻意不可区分）。

### 4.11 推送偏好

`GET /v1/push/preferences`

响应（早报固定默认、不可关闭）：

```json
{
  "morning_report_enabled": true,
  "weekly_enabled": true,
  "weekly_day_of_week": 1,
  "weekly_time": "09:00",
  "monthly_enabled": false,
  "monthly_day_of_month": null,
  "monthly_time": null,
  "content_items": ["wage_detail_push"]
}
```

`PUT /v1/push/preferences`（请求体为上述可写字段的子集；`content_items` 超出当前角色
可订阅范围返回 `422`）：

- 早报不可通过本接口关闭（`morning_report_enabled` 恒为 `true`）。
- 内容项角色过滤：`GET /v1/push/preferences/options` 返回当前角色可订阅项
  （`item_id` / `title` / `capability_id`）；员工通常仅工资明细推送，管理/老板按能力-角色
  矩阵可见更多项。
- 周/月推送为未启用的订阅偏好：真实定时推送通道待客户确认（开放决策），本版本不产生
  无人值守定时任务。

### 4.12 每日早报（按需生成）

`GET /v1/push/morning-report`

用调用者本人的实时凭据取数，按角色数据范围生成**昨日产量/工资摘要**并通过本地通道记录
投递。响应：

```json
{
  "role": "employee",
  "date_label": "2026-09-02",
  "sections": [
    { "capability_id": "FR-001", "title": "FR-001", "row_count": 3, "lines": ["..."] }
  ],
  "body": "…",
  "delivered": true
}
```

- 摘要内容即用户有权查看的数据；投递记录只保留信封（摘要哈希/行数），不留全文。
- 无真实推送通道前以"按需生成 + 站内/通道记录"交付；真实通道与无人值守 08:00 推送需
  客户确认通道与任务凭据机制后接入。

### 4.13 历史会话列表

`GET /v1/conversations?limit=20&cursor=<opaque>`

- `limit` 默认 20，服务端夹紧到 1–100；`cursor` 取上一页的 `next_cursor`，原样回传
  （不透明字符串，不要解析或构造）。
- 排序：`updated_at DESC, session_id DESC`（最近有活动的会话在前）。
- 响应：`{ "items": [ConversationSummary], "next_cursor": string | null }`。
  `items: []` 是合法的「该用户还没有历史会话」，**不是错误**（不会 404）。
- 当前身份**全部**会话都在列表内，包含**从未提问的空会话**（`interaction_count = 0`），
  并且**不按角色过滤**（角色由凭据决定且相对稳定，会话归属只看「租户 + 用户」）。
- 列表**不返回消息正文**（只有一行预览），避免一次拉回全部历史；正文按需走 §4.15。

`ConversationSummary`：

```json
{
  "session_id": "sess_2f1c9b748a3e4d6f9c216c1f0a7b5e10",
  "title": "我这个月的工资是多少",
  "created_at": "2026-09-21T02:15:31+00:00",
  "updated_at": "2026-09-21T02:18:04+00:00",
  "message_count": 7,
  "interaction_count": 2,
  "last_message": {
    "role": "assistant",
    "kind": "result_table",
    "text": "已返回 1 行结果。",
    "created_at": "2026-09-21T02:18:04+00:00"
  },
  "last_status": "completed"
}
```

- `title`：服务端**确定性派生**（取会话最早的 `role=user`/`kind=plain_text` 问句，折叠空白后
  截断 30 个字符 + `…`），**不可编辑、不调用模型**；无可用消息时为 `null`，
  前端显示「新对话」。
- `message_count` **不含 `kind="phase"`**（与前端渲染口径一致）。
- `last_message`：会话里**最新一条非 `phase` 消息**（`role` / `kind` / `text` / `created_at`），
  空会话为 `null`。它的 `kind` 可能是 `thinking`（该轮推理原文），列表预览时应改用
  「思考过程」类占位文案，不要把推理原文当摘要展示。
  `last_message.text` 是预览文本，最长 200 字符（超出截断补 `…`），完整正文见 §4.15。
- `last_status`：最近一轮的 `status`（§3.2），空会话为 `null`。

### 4.14 新建会话

`POST /v1/conversations`

请求体**可选**：`{"session_id": "2f1c9b74-..."}`
（1–128 字符，仅 `[A-Za-z0-9_-]`；**不传则由服务端生成**并返回）。

- 首次创建返回 **`201`**；**幂等**：同一身份下 `session_id` 已存在时返回 **`200`** 与
  **该会话当前的完整详情**——不新建、不报错、不清空已有消息。连点「新对话」或网络重试都安全。
- 响应体就是 §4.15 的 `ConversationDetail`，因此**「新建并展示整个会话流」一次请求即可完成**，
  前端拿到响应后不需要再调 §4.15。
- 单用户会话数上限 **500**（含空会话）：超出时**不新建**，返回
  `400 {"detail": "conversation limit reached"}`。上限只在**建档时**校验，
  **不影响既有会话继续提问**。本期无删除接口，提示文案不要写「请删除旧会话」。
- 只写会话元数据，**不调用客户 MES、不调用模型、不产生用量计量**。

### 4.15 会话详情（整个会话流）

`GET /v1/conversations/{session_id}?limit=50&cursor=<opaque>&exclude_kinds=phase`

- `limit` 默认 50、上限 200；`exclude_kinds` 为逗号分隔的**排除**列表（只做减法，
  如 `phase` 或 `phase,thinking`），出现未知取值返回 `400`（`unknown message kind: ...`）。
- 响应：

```json
{
  "conversation": { "...": "ConversationSummary（§4.13）" },
  "interactions": [ { "...": "ConversationTurn" } ],
  "messages": [ { "...": "MessageView（§3.3）" } ],
  "next_cursor": null
}
```

- `interactions` 按时间**升序**且**不分页**；`messages` 按 `(created_at, interaction_id, sequence)` 升序，
  `next_cursor` **只表示 `messages` 还有下一页**（翻页仍用本接口的 `limit` / `cursor`）。
- `ConversationTurn`：`interaction_id` / `status`（§3.2）/ `state` /
  `capability_id`（未命中能力时为 `null`）/ `created_at` /
  `completed_at`（**非终态时为 `null`**）/ `error_category`（非失败为 `null`）。
- `exclude_kinds` **只影响 `messages`**；`conversation.message_count` 口径不变，不会漂移。
- 会话不存在**或不属于当前身份** → `404 {"detail": "conversation not found"}`（两者刻意不可区分）。
- 本接口是 §4.5 的**超集**：§4.5 保持可用且行为不变（仅新增 §3.3 的两个消息字段），
  但新前端推荐统一使用本接口，避免两套读取路径的渲染差异。
- C1 / C2 / C3 的响应**稳定返回所有键**（`null` 也显式返回），可直接按固定 TS 类型消费。

> 面向前端的完整交付文档（TS 类型、调用封装、推荐接入流程、联调自查清单）：
> `docs/api/会话接口-前端对接.md`。

### 4.16 当前身份（角色与数据范围）

`GET /v1/me`

无请求体、无查询参数。响应为**该凭据对应的身份与授权范围**：

```json
{
  "uid": "10086",
  "uname": "张三",
  "role": "manager",
  "role_code": "02",
  "dept_ids": ["001", "005"],
  "data_scope": {
    "employee_ids": ["10086"],
    "dept_ids": ["001", "005"],
    "mes_filtered": false,
    "scope_version": "scope-00000001"
  }
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `uid` | string | 工号（token `user`） |
| `uname` | string | 姓名（token `uname`，权威来源；降级部署无 token 网关时回退到 §4.9 保存的展示名） |
| `role` | string | 领域角色：`employee` / `group_leader` / `manager` / `owner` |
| `role_code` | string | 客户角色码：`00` / `01` / `02` / `99`（与 §1「角色」一致） |
| `dept_ids` | string[] | token 返回的绑定部门/车间集合（00/01 一个元素；02 为绑定的全部部门，可跨车间；已排序） |
| `data_scope.employee_ids` | string[] | 本地可证的最小范围＝本人 |
| `data_scope.dept_ids` | string[] | 与顶层 `dept_ids` 同值 |
| `data_scope.mes_filtered` | boolean | `true` 表示更宽的行级可见范围由客户 MES 施加（99 老板＝全厂），本地不声称持有该范围 |
| `data_scope.scope_version` | string | 本次范围计算的版本标记，仅用于对账与排障，不参与前端判断 |

- **用途**：前端只持有加密 `app_key` 时，用它一次性拿到**权威角色**与数据范围，用于
  角色化 UI、范围提示与入口显隐；不需要、也不允许由前端声明角色。
- **与 `GET /v1/quick-questions` 的分工**：本接口回答「我是谁、我的范围是什么」，
  §4.6 回答「我有哪些可直接点的能力」。两者都由服务端权威判定；能力可用性以 §4.6 为准，
  前端不要自行用 `role_code` 拼能力开关（§7、附录 B）。
- **数据范围语义**：`dept_ids` 是**本地持有的最小可证范围**，不等于 MES 最终放行的行集；
  真正的行级过滤由客户 MES 完成。`mes_filtered` 就是这个事实的显式标记。因此
  「02 管理看哪些车间」以 `dept_ids` 为准，「99 老板看全厂」以 `mes_filtered` 为准。
- **失败语义**：凭据缺失/无效 → `401`；租户被停用 → `403`（同 §4 全部业务接口）；
  凭据已受理但无法解析出成员关系 → `403`。**降级模式（未配置 token 网关）下本接口不返回
  伪造身份**，会以 `403` 拒绝——该模式仅供联调，生产恒走 `X-Factory-Credential`。
- 本接口**只读**，不写任何状态；前端可在打开面板时调用一次并缓存，凭据变更时重新获取。

## 5. SSE 事件

### 5.1 帧格式与断点续传

每条事件一帧：

```text
id: {sequence}
event: {事件名}
data: {JSON}

```

- `id` 为持久化的单调递增序号（durable），从 1 开始；前端应记录最后收到的 `id`。
- 断线重连时携带 `Last-Event-ID: {最后收到的 id}`，服务端只重放其后的事件；缺失或
  非法的 `Last-Event-ID` 从头重放。
- 同一 interaction 的执行是幂等单跑的：只有一个连接真正执行取数，其他连接（重连、多开）
  只做事件跟随与重放，不会重复调用业务接口，因此重连是安全的。
- `interaction.heartbeat` 仅作保活（约 15 秒一次，出现在跟随场景），前端忽略即可，
  不要渲染。建议前端自行设置读超时，超时即按上述方式重连。

事件全集（`event` 取值）：

| 事件名 | 说明 | 是否终态 |
| --- | --- | --- |
| `interaction.started` | 本轮开始 | 否 |
| `interaction.phase` | 阶段**完成**推进（`status` 为 `ok`） | 否 |
| `interaction.progress` | 某阶段**进行中**（`status` 为 `running`），用于填充长耗时阶段的等待 | 否 |
| `interaction.thinking` | 推理原文分片（思考过程增量），按到达顺序拼接 | 否 |
| `interaction.clarification` | 追问补全参数 | 否 |
| `interaction.answer` | 闲聊应答全文（问候/闲聊/常识问答） | 否 |
| `interaction.result` | 结果卡片数据 | 否 |
| `interaction.heartbeat` | 保活 | 否 |
| `interaction.completed` | 本轮正常结束（结果就绪或进入追问） | 是 |
| `interaction.failed` | 本轮失败（含权限不足友好拒绝）；`data.message` 携带面向用户的中文文案，建议直接展示，不要只显示「查询失败」 | 是 |
| `interaction.cancelled` | 本轮被用户取消 | 是 |

> **前向兼容**：事件名集合此后仍可能扩充（本表即新增过 `interaction.progress`）。前端必须
> 忽略无法识别的 `event` 值，只处理自己认识的类型；未知事件不得报错、不得终止流。

> **越权拒绝（`error_category=scope_forbidden`）**：用户请求超出其角色可查询范围的数据时
> （如普通员工问「全组的工资明细」），服务在**任何业务取数之前**直接拒绝并返回友好文案，
> 例：「抱歉，您没有权限查询全组的工资明细。您当前可查询的范围是：本人的产量与工资数据。」
> 该文案同时持久化为 `error` 消息，历史回放可见。前端对 `interaction.failed` 应优先展示
> `data.message`。

### 5.2 过程事件

`interaction.started`：

```text
id: 1
event: interaction.started
data: {"interaction_id":"it_xxx","session_id":"sess_xxx","state":"parsing","stage":"接收","status":"accepted"}
```

前端处理：保存 `interaction_id`（虽然发起时已返回），当前输入进入加载态。

`interaction.phase`：

```text
id: 2
event: interaction.phase
data: {"state":"authorizing","reason":"intent_complete","stage":"鉴权","status":"ok","duration_ms":420}
```

`state` 取值见 §3.2；`stage` 为中文阶段名（接收/解析/追问/鉴权/取数/计算/完成/失败/取消），
可直接用于进度条文案。典型顺序：`authorizing`（鉴权）→ `executing`（取数）→
`composing`（计算）。该事件表示阶段**已完成**（`status` 为 `ok`）。

`interaction.progress`（阶段进行中）：

```text
id: 2
event: interaction.progress
data: {"state":"parsing","reason":"parse_started","stage":"解析中","status":"running","duration_ms":180}
```

一轮问答中有三段耗时工作不会产生任何其他过程事件：意图解析、权限校验（含范围守卫的模型
调用）、过滤条件的目录解析。每段开始前各发一条 `interaction.progress`，前端可据此把「加载中」
替换成正在做的具体事情，避免长等待期间界面无信息可展示。

- `stage` 固定文案，按顺序出现：`解析中` → `权限检查中` → `核对数据范围`，可原样展示。
- `reason` 为机器可读的起点标识（`parse_started` / `authorize_started` /
  `scope_resolution_started`），可用于埋点与耗时对比。
- `state` 是**当前真实状态**，这三条都为 `parsing`：整条鉴权链在状态机仍处于解析阶段时完成，
  进度事件不推进状态机。请用它旁边的 `stage` 渲染文案，不要用 `state` 反推阶段。
- `duration_ms` 与 `interaction.phase` 同义：自本轮运行起的累计耗时，不是与上一阶段的间隔。
- 闲聊与追问轮次只会出现 `解析中`：这两条路径没有任何业务取数。
- 随后照常收到 `interaction.phase`（`stage` 为 `鉴权`/`取数`/`计算`），表示对应阶段已结束。

`interaction.thinking`（推理原文，实时思考过程）：

```text
id: 3
event: interaction.thinking
data: {"text":"正在理解您的问题，确认要查询的内容。","seq":1,"done":false,"elapsed_ms":180}

id: 4
event: interaction.thinking
data: {"text":"\n本次查询：个人产量统计。","seq":2,"done":false,"elapsed_ms":640}

id: 8
event: interaction.thinking
data: {"text":"","seq":6,"done":true,"elapsed_ms":12400}
```

用户提问后到结果卡片之间的等待，分为两种：**模型正在判断**（意图解析、权限校验）和
**工厂系统正在返回数据**（取数）。两者都不产生其他过程事件，用户在十几秒内看不到任何变化。
`interaction.thinking` 用**旁白**填满这段等待：内容是对当前正在做什么的实时描述，随生成
逐片下发，前端拼接后即为完整思考原文。分片可能来自模型（措辞自由，事实受限），也可能来自
服务端已确定的事实（阶段名、查询标题、时间范围、可查询范围、取数分页进度），两者拼在同一条
`text` 里。

字段：

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `text` | `string` | 非最后一片必填 | 本片**增量**（delta），非全量。前端按**到达顺序拼接**，勿按 `seq` 重排。单帧 ≤200 字符，整轮累计 ≤8000 字符。换行已转义为 `\n` |
| `seq` | `integer` | 是 | 分片序号。同一轮内从 **1** 开始、严格递增、不跳号不重复。前端据此去重（重复 `seq` 只保留首次） |
| `done` | `boolean` | 最后一片必须为 `true` | 是否为本轮推理的最后一片 |
| `elapsed_ms` | `integer` | 否 | 自本轮开始至今的**累计**思考耗时（毫秒），不是与上一片的间隔 |
| `truncated` | `boolean` | 否 | 仅最后一片可能为 `true`：累计长度达上限被截断，此时建议提示「推理过程已截断」 |

前端处理要点：

- **默认收起**，折叠态只显示一行「思考过程 · Ns」（`Ns` 由 `elapsed_ms` 换算）；收到
  `done:true` 或 `interaction.result` 后自动收起。点击展开/收起，**所有角色一致，不做权限区分**。
- 流式期间可自动展开并滚动到底部；建议按约 **80ms** 合并分片批量刷新，不必逐帧重绘。
- 三种合法组合：非空 `text` + 无/`false` `done` → 拼接；空 `text` + `true` → 标记结束；
  非空 `text` + `true` → 拼接后结束。**空 `text` 且 `done` 不为 `true` 的帧请忽略**，不要新建消息。
- 帧的 `id:` 单调递增**先于** `interaction.result` 的 `id`；本轮结束后不会再收到该轮的
  `interaction.thinking`。刷新页面后，完整原文来自 §4.5 历史消息中 `kind="thinking"` 的消息
  （§3.3），默认同样收起。
- 该事件**不进 xlsx 导出**，与结果卡片无关。默认**每一轮都会发**（含闲聊与追问轮）；仅当服务端
  关闭该功能时才完全不出现，因此前端应把它当作**可选**事件处理，不能在缺少它时阻塞渲染。

> **关闭开关**：服务端配置 `session_thinking_enabled=false` 时整轮不再下发
> `interaction.thinking`，也不会写入 `kind="thinking"` 消息。前端无需感知，按上述「无则忽略」
> 处理即可。

`interaction.clarification`（缺时间、款号、小组等条件时的追问）：

```text
id: 3
event: interaction.clarification
data: {"question":"请问您要查询哪个时间范围的产量？","missing":[],"ambiguous":[]}
```

前端处理：展示追问气泡；随后会收到 `interaction.completed`（`status` 为
`"clarifying"`）结束本轮。用户补充信息后作为新一轮提问重新走 §4.2。追问轮次有上限
（默认 3 轮），超出后本轮以 `interaction.failed`（`error_category` =
`clarification_exhausted`）收尾。

### 5.3 结果卡片数据契约（`interaction.result`）

```text
id: 5
event: interaction.result
data: {"capability_id":"fr009_factory_order_overview","columns":["order_code","plan_code","plan_missing","order_count","sheet_count","package_count","issued_qty","received_qty","progress_ratio","progress_label","worktype_count","finished_sheet_count","unfinished_sheet_count","unavailable_sheet_count","finish_state"],"column_titles":{"order_code":"计划号","plan_code":"计划号标识","plan_missing":"未关联计划号","order_count":"订单统计组数","sheet_count":"制单数","package_count":"包数","issued_qty":"发卡数量","received_qty":"实收数量","progress_ratio":"进度数值","progress_label":"进度","worktype_count":"工序数","finished_sheet_count":"已完工制单","unfinished_sheet_count":"未完工制单","unavailable_sheet_count":"无法判断制单","finish_state":"状态"},"row_count":12,"incomplete":false,"incomplete_reason":null,"artifact_id":"art_xxx","answer":"查询完成，共 12 行结果，详情请查看下方结果卡片。"}
```

字段：

| 字段 | 说明 |
| --- | --- |
| `capability_id` | 本次执行的能力（recipe 形式 id，与附录 A 的 FR 编号一一对应，如 `fr009_factory_order_overview` ↔ FR-009） |
| `columns` | 结果列名列表（字符串数组），列序即报表列序；`name` 为稳定标识符（与导出文件、日志一致），**不是给工人看的文案** |
| `column_titles` | 列名 → 中文展示名（如 `rq`→`日期`、`je`→`小计金额`）的映射；**面向用户展示列名时一律用此映射**（含导出文件表头，导出表头形如 `产量（件）`）；缺失某列时回退用 `columns` 中的原名 |
| `row_count` | 结果行数 |
| `cache_source` | 可选的缓存来源元数据：`{sources: ("query" \| "precompute")[], generated_at: string[]}`；数组去重并排序，时间为带时区 ISO 8601。`precompute` 表示预取底料，`query` 表示查询完整快照。不含客户行、原始缓存键或身份；可缺席，缺席不能仅据此判断某个辅助目录是否命中。该字段不改变结果、权限或下钻请求格式 |
| `incomplete` | 结果是否不完整（如分页拉取异常、个别指标不可用）；为 `true` 时必须向用户展示不完整提示 |
| `incomplete_reason` | 不完整原因（如 `pagination_*`、`metric_unavailable:*`），`null` 表示完整 |
| `artifact_id` | 导出产物 ID；为 `null` 表示本次未生成导出，不展示导出按钮 |
| `answer` | 由模型结合用户问题与结果元数据生成的一句自然语言回答，可直接展示在结果卡片上方；**空结果（`row_count=0` 且 `incomplete=false`）表示该时间范围内没有查询到相关记录，属正常结果，前端不应展示"不完整"样式**。字段不出现或为空时按旧形态兼容处理 |
| `time_range` | **本轮真实取数窗口**（2026-09-20 D-7 新增，可缺席）：`{start, end, label, expression?}`。`start`/`end` 是服务端**执行时实际使用**的规范半开 UTC 区间（ISO 8601 带时区，`end` 为排他上界）；`label` 为工厂本地包含式展示对（如 `2026-09-15 至 2026-09-18`），**建议直接显示在卡片标题旁**；`expression` 仅在窗口由受审时间短语解析而来时出现（如 `上个月`）。**下钻时前端应把 `start`/`end` 原样回传给 `drill.time_range_start` / `drill.time_range_end`（§4.2）**，否则下钻会落到另一个窗口。空结果轮次同样带此字段（供同会话后续轮次继承） |

事件携带结果元数据（列定义、行数、完整性、导出入口），并可选携带结构化卡片 `card`
（§5.3.1，含 KPI 区与预览行）；预览行以外的行级明细数据在导出文件中。持久化的
`result_table` 消息（§3.3）是该结果的历史形态，`text` 为结果摘要
（如"已返回 12 行结果。"）；回答文本同时以 `role=assistant`、`kind=plain_text`
消息持久化。

**进度口径**：订单/款号进度及包级进度的「进度」列 = **实收数量 ÷ 发卡数量 × 100**，
按同一范围的分子、分母合计重算，分母为零返回零，保留两位小数。展示时遵循服务端卡片的单位和
`notes`，不得从包数比例、各行百分比的平均或工序行数另算进度。

FR-001～FR-013 及现有下钻均可使用服务器两族公共明细缓存；范围过滤和本地计算由服务器完成。
当前窗口首次冷态、缓存不可达、数据不完整或最终 shadow 不一致时会回源，客户端无需增加缓存模式
参数。shadow 验证在同一个服务器沙箱连接内比较最终结果和卡片，不一致时返回实时结果并驱逐坏键。

**已移除的列（2026-09-15 口径重梳）**：`customer_name`（客户名称）、`plan_qty`（计划数量）、
`completed_qty`（原「完工数量」，实为投产量）、`delivery_warning`（交期预警）、`days_remaining`（距交期）
**全部取消** —— 其唯一来源「生产计划接口」已弃用，制单侧无替代字段。客户给出新字段后再议。
`plan_qty` 的位置由 `cut_qty`（裁剪数量）承接；`completed_qty` 由 `finished_packages`（完工包数）承接。

#### 5.3.1 结构化卡片（可选增量字段 `card`）

`interaction.result` 的 `data` 可携带可选字段 `card`（结构化结果卡片），
持久化的 `result_table` 消息 `payload.card` 与其为**同一份数据**（实时 / 重放 /
历史三路一致）。该字段**可能缺席**：无 `card` 字段时按旧形态（只有元数据 + 导出入口）
兼容渲染，**前端必须先判空再渲染**。

```json
{
  "kind": "ranking",
  "title": "员工工资清单与排名",
  "capability_id": "fr008_payroll_ranking",
  "metrics": [ { "label": "工资金额", "unit": "元", "value": "321000" } ],
  "table": {
    "columns": ["uid", "uname", "dept", "package_count", "gross", "rank_position", "group_rank"],
    "column_titles": { "uid": "工号", "uname": "姓名", "dept": "车间/小组", "package_count": "计件件数", "gross": "工资金额", "rank_position": "名次", "group_rank": "组内名次" },
    "rows": [ ["0020", "王林", "001", "120", "6200", "1", "1"] ],
    "total_rows": 137,
    "preview_max_rows": 10,
    "truncated": true,
    "page": 1,
    "page_size": 10,
    "has_more": true,
    "group_by": "dept",
    "groups": [ { "group": "001", "name": "缝纫一组", "row_count": 10, "total_rows": 25, "truncated": true } ],
    "groups_total": 7
  },
  "chart": {
    "type": "bar",
    "unit": "件",
    "granularity": "day",
    "points": [ { "label": "09-08", "value": "2460" }, { "label": "09-09", "value": "2670" } ]
  },
  "actions": [
    { "type": "drill", "label": "查看该成员的工资", "capability_id": "fr012_employee_payroll", "bind_from": { "employee_uid": "uid" } }
  ],
  "time_range": { "start": "2026-09-14T16:00:00+00:00", "end": "2026-09-18T16:00:00+00:00", "label": "2026-09-15 至 2026-09-18" },
  "totals": [ { "label": "工资金额", "unit": "元", "value": "321000" } ],
  "unavailable_columns": ["日均工资"],
  "notes": ["本次结果不完整（pagination_total_drift），以导出文件为准。"]
}
```

字段说明（除 `table` 外均可缺席）：

| 字段 | 说明 |
| --- | --- |
| `kind` | 卡片形态：`kpi`（仅 KPI 数字区，无 `table`）；`table`（KPI + 明细预览表）；`ranking`（排名表，服务端已排好序） |
| `title` | 卡片标题（能力中文名） |
| `metrics` | KPI 数字区：`{label, value, unit}`；无数据源时为 `{label, unit, unavailable: true}`（渲染"暂无数据源"，**不得渲染为 0**） |
| `table` | 明细预览表；`rows` 为**二维数组**（列序 = `columns` 序），只含预览行（仅前 N 行，完整数据走导出 xlsx）。空单元格为 `null`，渲染"—" |
| `table.total_rows` / `truncated` / `preview_max_rows` | 行数与截断信息：`truncated=true` 时展示"仅展示前 N 行（共 M 行），完整数据请导出"类提示 |
| `table.group_by` / `groups` / `groups_total` | **分组卡**（组长/老板工资名单）：按 `group_by` 列分组、组内已按金额降序；`groups[]` 每项 `{group, name, row_count, total_rows, truncated}`（`name` 为组显示名，可缺席，缺席时用 `group` 原值）。同组成员在 `rows` 里**连续存放、段落顺序与 `groups[]` 一致**，前端按每组 `row_count` 顺序切片即可（tab 切换 + 组内展开成员，**不需要自己重新分组**）；建议每个 tab 文案为「组名（本组总人数）」 |
| `table.page` / `page_size` / `has_more` | **分页三字段**（总是同时下发）：预览模式下固定 `page=1`，`page_size` = 预览行上限，`has_more` 派生自 `truncated`；完整行走导出文件 |
| `chart` | **图表区**（可缺席）：`type` 本期仅 `"bar"`（枚举位预留）；`granularity` 为 `day`/`week`——日粒度点数超过 62 时服务端自动降为周粒度（`label` 为周起始日），**前端零聚合逻辑**；`points[]` 每项 `{label, value}`，`label` 为原始日期（`MM-DD`），本地化展示由前端负责；`points` 已按服务端排好序；无数据时整个 `chart` 区块缺席（不下发空数组） |
| `actions` | **卡片级下钻声明**（可缺席）：`{type: "drill", label, capability_id, bind_from, clear_fields?}`；`bind_from` 为「下钻槽位键 → 结果列名」映射。先复制 `drill_context` 并清除 `clear_fields` 指定的旧业务条件，再从被点行绑定目标值，发起新一轮请求（§4.2 / §5.3.2）；无权角色不会收到对应入口 |
| `time_range` | **本轮真实取数窗口**（2026-09-20 D-7 新增，可缺席）：与同一轮的 `interaction.result` 顶层 `time_range` **是同一份数据**（`{start, end, label, expression?}`，见 §5.3）。前端点击下钻时应把它原样回传给 `drill.time_range_start` / `drill.time_range_end`（§4.2）——这是「下钻进用户看到的那一个窗口」的唯一依据，`expression` 只是展示标签，服务端不会用它重新解算窗口 |
| `table.alert_marker` | 语义高亮：`{column, equals}`——行内该列值等于 `equals` 时整行标红。**当前无能力使用**（原 FR-009 交期预警随生产计划接口弃用而取消，2026-09-15）；字段保留以便将来有新的预警列时复用 |
| `totals` | 合计区（卡片底部），结构同 `metrics` |
| `unavailable_columns` | 无数据源的 KPI 列中文名列表 |
| `notes` | 面向用户的提示文本（口径说明、不完整提示等），逐条展示即可 |
| `order_groups` | 可选订单分组视图，当前 `version=1`；计划汇总与组内明细分别提供，不同于工资表的 `table.groups`，详见 §5.3.2 |
| `data_snapshot` | 订单分组数据版本标识 `{id: string}`，供组内翻页检测范围或底料版本变化；是 64 位小写十六进制不透明摘要，不是缓存键、权限凭据或保证长期保留的快照 |
| `drill_context` | 原查询的绝对时间、部门及订单/款号/计划/物料业务条件，普通卡片与订单分组卡片均可提供。用于跨能力下钻：先执行动作 `clear_fields`，再按被点行 `bind_from` 覆盖槽位，见 §5.3.2；它不包含父层视图、页码或版本选择 |

各能力默认卡形态（未列出的能力暂无 `card`，按旧形态渲染）：

| capability_id | 能力 | `kind` | KPI 区 | 预览表要点 |
| --- | --- | --- | --- | --- |
| fr002_personal_wage_summary | 个人工资汇总 | `kpi` | 计件工资合计 / 计件件数 / 日均工资 | 无 |
| fr012_employee_payroll | 任一员工工资查询（01 组长 / 02 管理 / 99 老板，2026-09-16 D-5 放开；目标员工必须在调用者可查范围内） | `kpi` | 计件工资合计 / 计件件数 | 无 |
| fr004_group_income_rank | 收入排名（我在组里排第几） | `kpi` | 金额 / 名次 / 组内名次 / 组内人数（单行） | 无 |
| fr003_personal_wage_detail | 个人工资明细 | `table` | 小计金额（合计） | 日期/款号/工序/完成数量/工价单价/小计金额 |
| fr001_personal_output | 个人产量统计 | `table` | 产量（合计） | 日期/款号/工序/产量 |
| fr006_order_output | 订单/款号产量 | `table` | 报工工作量 / 报工金额 / 全查询范围去重参与人数 | 一行一个计划号组，展开 `worktype_details` 查看真实款式与工序。工作量单位为 `件·工序`；工序人数、组人数不能相加成全范围人数；没有可比发卡分母时工序占比不可用 |
| fr005_order_progress | 订单/款号进度 | `table` | 制单数 / 包数 / 发卡数量 / 已完工制单 / 未完工制单 / 无法判断制单 | 一行一个计划号组，空计划号统一一组；展开 `sheet_details` 查看制单进度。进度为累计实收合计 ÷ 发卡合计 × 100，状态逐制单判断 |
| fr009_factory_order_overview | 全厂订单进度总览 | `table` | 制单数 / 包数 / 发卡数量 / 已完工制单 / 未完工制单 / 无法判断制单 | 与 FR5 使用相同计划号组与制单明细结构；组内差异在子表展示。无客户、交期及交期预警 |
| fr008_payroll_ranking | 员工工资清单与排名 | `ranking` | 工资金额（合计） | 工号/姓名/车间小组/计件件数/工资金额/名次/组内名次；**`group_by: dept` 按组 tab 切换**；**actions（点成员行 → fr012 工资条，01/02/99 均下发）** |
| fr011_factory_payroll_stats | 全厂工资统计 | `table` | 工资总额 / 人均工资 / 在册人数（aux KPI） | 车间小组/应发合计/在册人数/人均工资；**actions（点车间行 → fr008 工资清单）** |
| fr007_workshop_output_comparison | 车间产量对比 | `ranking` | 报工产量（合计） | 车间小组/报工产量/参与人数/人均产量/名次；**可见部门数为 1 时 `rank_position` 取值不可用（单元格为 `null`，渲染「—」/暂无数据源）**，列本身仍下发、卡片仍是 `ranking` 形态（1 行），`notes` 说明「无对比对象」 |
| fr010_workshop_output_overview | 小组/车间产量 | `table` | — | 车间小组/款号/报工产量/参与人数/人均产量；**无计划数量、无完工数量、无达成率** |
| fr013_factory_output_dashboard | 全厂产量总览（综合看板，仅老板） | `ranking` | 总产量 / 在产制单 / 人均月产（aux KPI） | 车间小组/报工产量/参与人数/人均产量/名次 + 日产量柱状图 + 点车间行查看 FR10 产量明细；在产制单按不同真实制单号计算，不能解释为计划号订单数；报工产量为 `件·工序` |

进度查询采用「计划号汇总 → 组内制单 → 制单包明细 → 物料工序明细」。同能力组内展开与翻页、
切换到包或工序能力均是新的问答轮次，真实业务标识与上下文的传递见 §5.3.2。

空结果（`row_count=0` 且 `incomplete=false`）**不下发 `card`**，前端按"没有查询到相关记录"空态处理；`incomplete=true` 时 `card` 照常下发，并附 `notes` 提示。

`interaction.completed`（终态）：

```text
id: 6
event: interaction.completed
data: {"interaction_id":"it_xxx","status":"completed"}
```

注意：`data.status` 有两种取值——`"completed"`（结果就绪）与 `"clarifying"`
（本轮以追问收尾，等待用户补充后发起新一轮）。

#### 5.3.2 订单分组、展开与组内分页（`card.order_groups.version=1`）

FR5/FR9 的主结果按计划号汇总，组内是制单进度；FR6 的主结果也按计划号汇总，组内是款式与
工序报工明细。空计划号只产生一个未关联组，组内仍保留全部制单或工序记录。
前端优先使用 `order_groups` 渲染“汇总区 + 可展开明细”，不在父级摆放无法代表整组的制单号、
床号和日期。标题可组合为“未关联计划号 · 3 张制单”，数字取该组 `summary`，不能从预览行数计算。
只有一个组时默认展开，否则展示各组汇总；制单表提供“只看未完工”入口。
组内 `created_date` 是服务端验证并标准化后的真实制单日期；ISO 日期时间取字段自身的业务日期，
不按时区偏移移动到另一日。
前端直接显示该列，不解析制单编号推断日期。未知日期保持不可用，不能从编号或提问文本补写。

| 字段 | 契约 |
| --- | --- |
| `order_groups.version` | 当前为 `1`；未知版本兼容展示普通主表与导出入口 |
| `groups_total` / `page` / `page_size` / `has_more` | 当前范围内的统计组总数、当前组列表页、每页组数及是否有下一页；默认每页 50 组。完整组在导出中，组列表也可按服务端模板继续翻页 |
| `order_groups.request` | 下一组列表页的同能力 `drill` 模板，包含 `group_page`、`group_page_size`、`data_snapshot_id`、绝对时间与原筛选；只有 `has_more=true` 时展示下一页入口 |
| `groups[].id` / `name` | `id` 是稳定组标识，`name` 仅用于显示；空组 ID 为 `unlinked`，真实计划号 ID 为摘要。相同显示名不能作为合并依据 |
| `groups[].kind` / `plan_code` | `kind` 为 `plan` 或 `unlinked`。`plan` 携带真实原始计划号，非空值保留原值；`unlinked` 的 `plan_code=null`，统一覆盖 NULL、空串及纯空白 |
| `summary` | 已计算的指标或状态数组 `{name,title,type,unit,value}`；未知值为 `unavailable:true`。不重复隐藏的内部组键 |
| `state_counts` | 制单状态分布 `{finished,unfinished,unavailable}`，值为已计算数值文本或 `null`；FR6 没有制单状态分布时为 `{}` |
| `default_expanded` | 当前结果只有一个统计组时为 `true` |
| `views[]` | 每项 `{view_id,title,table,request}`；FR5/FR9 注册 `sheet_details`，FR6 注册 `worktype_details` |
| `views[].table` | `columns`、`column_titles`、二维 `rows` 沿用现有表结构；`column_metadata` 每列包含 `{name,title,type,unit}` |
| `total_rows` / `filtered_total_rows` | 同一组明细的完整行数 / 状态筛选后的完整行数；均在分页之前计算 |
| `status` / `page` / `page_size` / `has_more` | 状态筛选、当前页、每页条数、是否有下一页；先筛选后分页。默认第一页 10 行，每页最多 100 行，页码 1–100000 |
| `unavailable_columns` | 整组均未知的列从显示列移除，在此提供完整列元数据；部分行未知仍显示 `null`。不要补零或根据第一页推断整列可用性 |
| `table.actions` | 该明细表的审核下钻动作，结构为 `{type:"drill",label,capability_id,bind_from,clear_fields?}`；清除旧业务条件后再绑定被点行 |
| `views[].request` | 可直接放入下一轮请求 `drill` 的模板，含同一能力、绝对时间、原业务条件和 `order_view`。修改页码或状态时保留其它字段 |

`drill.order_view` 的字段：

| 字段 | 约束 |
| --- | --- |
| `view_id` | 必填，已登记的明细视图名，1–64 字符的普通标识符；必须与该能力匹配 |
| `group_kind` | 必填，`plan` / `unlinked` |
| `plan_code` | `plan` 必须提供非空真实计划号（最长 128 字符）；`unlinked` 必须为 `null` 或缺席，不能发送中文标签 |
| `page` / `page_size` | 默认 1 / 10，范围见上表；必须为整数，布尔值或数字字符串不接受 |
| `status` | `all` / `finished` / `unfinished` / `unavailable`，默认 `all`；状态筛选仅用于注册了状态列的制单明细，工序报工视图不支持 |
| `snapshot_id` | 64 位小写十六进制摘要；第一页可缺席，第 2 页起必填。服务端模板总是给出，前端展开、筛选、翻页时必须原样保留，不自行生成 |

组列表翻页使用 `drill.group_page`（整数 1–100000）、`group_page_size`（整数 1–50）及
`data_snapshot_id`（64 位小写十六进制摘要）。第 2 页起必须回传摘要和绝对时间，否则请求结构
校验返回 422；不能将具体 `order_view` 与组列表第 2 页混用。明细第 2 页起缺少 `snapshot_id`
也返回 422。所有翻页只在原能力、原范围和原窗口内执行。

匿名合成响应节选（省略普通卡片指标、主表及其它列，数字仅用于说明契约）：

```json
{
  "card": {
    "data_snapshot": {"id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"},
    "drill_context": {
      "time_range_start": "2026-08-31T16:00:00+00:00",
      "time_range_end": "2026-09-30T16:00:00+00:00",
      "order_codes": [], "style_codes": [], "plan_codes": [], "material_ids": [], "dept_ids": []
    },
    "order_groups": {
      "version": 1, "groups_total": 1, "page": 1, "page_size": 50, "has_more": false,
      "groups": [{
        "id": "unlinked", "name": "未关联计划号", "kind": "unlinked", "plan_code": null,
        "summary": [{"name": "sheet_count", "title": "制单数", "type": "quantity", "unit": "单", "value": "3"}],
        "state_counts": {"finished": "2", "unfinished": "1", "unavailable": "0"},
        "default_expanded": true,
        "views": [{
          "view_id": "sheet_details", "title": "制单进度",
          "table": {
            "columns": ["sheet_code", "finish_state"],
            "column_titles": {"sheet_code": "制单号", "finish_state": "状态"},
            "column_metadata": [{"name": "sheet_code", "title": "制单号", "type": null, "unit": null}, {"name": "finish_state", "title": "状态", "type": null, "unit": null}],
            "rows": [["SAMPLE-SHEET-1", "未完工"]],
            "total_rows": 3, "filtered_total_rows": 3,
            "status": "all", "page": 1, "page_size": 1, "has_more": true,
            "unavailable_columns": [],
            "actions": [{"type": "drill", "label": "查看包明细", "capability_id": "fr005_order_package_detail", "bind_from": {"order_codes": "sheet_code"}}]
          },
          "request": {
            "capability_id": "fr009_factory_order_overview",
            "time_range_start": "2026-08-31T16:00:00+00:00", "time_range_end": "2026-09-30T16:00:00+00:00",
            "order_codes": [], "style_codes": [], "plan_codes": [], "material_ids": [], "dept_ids": [],
            "order_view": {"view_id": "sheet_details", "group_kind": "unlinked", "plan_code": null, "page": 1, "page_size": 1, "status": "all", "snapshot_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
          }
        }]
      }]
    }
  }
}
```

点击“只看未完工”：复制该 `view.request` 到 `drill`，仅把 `order_view.status` 改成 `unfinished`、
`page` 重置为 1。点击下一页：保留同一状态、窗口、筛选和 `snapshot_id`，仅增加 `page`。
请求示例：

```json
{
  "text": "查看未关联组的未完工制单",
  "drill": {
    "capability_id": "fr009_factory_order_overview",
    "time_range_start": "2026-08-31T16:00:00+00:00", "time_range_end": "2026-09-30T16:00:00+00:00",
    "order_codes": [], "style_codes": [], "plan_codes": [], "material_ids": [], "dept_ids": [],
    "order_view": {"view_id": "sheet_details", "group_kind": "unlinked", "plan_code": null, "page": 1, "page_size": 10, "status": "unfinished", "snapshot_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"}
  }
}
```

组列表超过一页时，复制 `order_groups.request` 到 `drill`，保留同一窗口和原筛选。
匿名下一组页请求示例（版本摘要仍应使用实际响应值）：

```json
{
  "text": "查看下一页计划号汇总",
  "drill": {
    "capability_id": "fr009_factory_order_overview",
    "time_range_start": "2026-08-31T16:00:00+00:00", "time_range_end": "2026-09-30T16:00:00+00:00",
    "order_codes": [], "style_codes": [], "plan_codes": [], "material_ids": [], "dept_ids": [],
    "group_page": 2, "group_page_size": 50,
    "data_snapshot_id": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
  }
}
```

点击某张制单的“查看包明细”：复制 `card.drill_context`，将 `capability_id` 改为动作目标，
先按动作 `clear_fields` 清除旧业务条件，再按 `bind_from` 用被点行真实 `sheet_code`
**覆盖** `order_codes`，包装为数组。
这是从计划号层切到制单层，不能把父层计划号和制单号追加到同一数组；不携带父层 `order_view`。
包表点击“查看工序”时，动作声明 `clear_fields:["order_codes"]`：**先清除父层作为制单号使用的
`order_codes`**，再用真实 `material_id` 覆盖 `material_ids`，保留原时间、部门、款式及仍适用
的计划条件。工序层的 `order_codes` 按计划号解释，不能把制单号继续带入；物料标识限定目标。
`clear_fields` 仅允许 `order_codes/style_codes/plan_codes/material_ids`，不允许清除
`dept_ids`、`employee_uid` 或任何身份/范围字段。跨能力下钻复制 `drill_context`，不复制
`views[].request` 或组列表模板，因此父层 `order_view/group_page/data_snapshot_id` 不继承。
同能力组内翻页则使用 `views[].request`，其模板保留当前实际状态和页码，前端只修改目标页码。
不发送“未关联计划号”、款式显示名或组摘要作为业务标识。

包卡上的动作示例：

```json
{
  "type": "drill", "label": "查看工序", "capability_id": "fr005_order_worktype_detail",
  "bind_from": {"material_ids": "material_id"}, "clear_fields": ["order_codes"]
}
```

每轮重新授权，业务条件只能收窄有效范围；历史卡片不保存访问权限。`snapshot_id` 与组列表的
`data_snapshot_id` 均回传 `card.data_snapshot.id`，绑定配方版本、
角色/租户/员工/部门有效范围、绝对时间、业务条件、完整获准取数内容及可用的底料生成版本。
服务端重新执行时版本不同，以 `interaction.failed` 的
`error_category=order_view_snapshot_changed` 终结，持久化提示“数据已刷新，请重新查看汇总后再展开明细。”。
前端引导重新查询汇总，不继续拼接两份版本的页；这不是 HTTP 409。
未知或失效组内视图在执行阶段以 `order_view_invalid` / `order_view_unavailable` 友好拒绝。
请求结构缺失、未注册视图或不支持的状态筛选仍在请求校验阶段返回 400/422。

前端不能对预览行求和、相加工序人数或重算全组进度。进度优先显示服务端 `progress_label`：
四舍五入到 100% 但实收仍不足时显示“接近100%（仍有未完工）”；超收相抵后合计达到 100% 但
仍有未完工制单时保留未完工说明，不将整个组标成全部完工。真实款式名和内部编码分别展示，
不能根据问句猜测二者对应。FR6 主卡参与人数在整个获准查询范围独立去重。
FR6 指定员工时，工资按该员工收窄；发卡资料不能认定为全订单的可比分母，工序占比保持
`unavailable`，不为获得分母扩大员工范围。所有工资来源能力均不适用 `material_ids`，结构化
物料筛选提前以 400 拒绝；进度能力则在获准默认明细上先筛选物料，再计算所选视图。

分页只切卡片预览，Excel 导出包含完整主汇总和完整明细工作表；指定明细状态时导出该状态匹配
的全部行，不只导出当前页。每个明细工作表前两列为“计划号”“关联状态”：空组显示
“未关联计划号 / 未关联”，真实计划号即使文字恰好为“未关联计划号”也显示“已关联”，
不凭显示名误合并。内部 `plan_code/plan_missing` 等元数据保持隐藏。

### 5.4 闲聊应答（`interaction.answer`）

当用户输入是问候、闲聊或与工厂业务无关的常识问答（例如"你好""介绍一下鲁迅"）时，本轮
**不会**执行任何业务能力、不经过鉴权/取数/计算、不产出结果卡片：意图选择器选中保留能力
`chitchat`，系统直接生成一段闲聊文本并经本事件送达。

```text
id: 5
event: interaction.answer
data: {"text":"你好呀！我是工厂助手，有什么可以帮你的吗？"}
```

- `text`：可直接展示的回复全文。涉及实时或无法核实的信息（如今天的天气、实时行情）时，
  回复会说明"无法获取实时数据"，不会编造，也不会查询任何工厂数据。
- 闲聊轮事件序列：`interaction.started` → `interaction.progress`（仅 `解析中`）→
  [`interaction.thinking`（若服务端开启，§5.2）] → `interaction.answer` →
  `interaction.completed`（`status` 为 `"completed"`），既无 `interaction.phase` 也无
  `interaction.result`；不占用追问轮次，不进入查询历史/收藏。
- 回复同时以 `role=assistant`、`kind=chat` 消息持久化，历史消息接口（§4.5）可取回。

### 5.5 失败与取消事件

`interaction.failed`（终态）：

```text
id: 4
event: interaction.failed
data: {"interaction_id":"it_xxx","error_category":"forbidden"}
```

友好文案随 `kind=error` 消息持久化（历史消息接口可取回），例如：

- 权限不足：`"当前角色暂不支持该查询。您可查询的范围：本人的产量与工资数据。"`
  （范围文案按角色给出，见附录 A）
- 超时间范围：`"时间范围超出上限（近一年）：请查询最近 366 天以内的数据。"`
- 一般失败：`"查询未能完成。"`

常见 `error_category`：

| 值 | 场景 |
| --- | --- |
| `forbidden` | 能力不在当前角色可用范围（能力-角色矩阵拒绝，附友好提示与可查范围） |
| `forbidden_*` | 执行层范围规则拒绝（如查询范围超出当前角色可查范围、越权绑定部门/员工等） |
| `filter_*` | 订单号/款号/小组等业务条件解析失败或超出可查范围 |
| `time_range_missing` | 缺少时间条件，且**同会话也没有可继承的窗口**（只有在没有任何前序结果可参照时才追问/终止） |
| `time_range_exceeds_limit` | 时间范围超过近一年上限（友好终止，不发起取数） |
| `drill_invalid_time` | 结构化下钻携带的时间窗口不可用（起止倒置 / 超上限 / 落在未来）；零业务调用，文案「下钻时间范围无法识别，请重新发起查询。」 |
| `order_view_invalid` / `order_view_unavailable` | 组内明细在当前查询范围内不可用，提示重新查看汇总；不回退为宽范围结果 |
| `order_view_snapshot_changed` | 原汇总与本轮底料/范围版本不一致，提示“数据已刷新，请重新查看汇总后再展开明细。”；不得继续拼接旧页 |
| `clarification_exhausted` | 追问轮次用尽 |
| `capability_unresolved` / `capability_unregistered` | 意图无法落到已注册能力 |
| `gateway_*` / `model_output_invalid` | 模型调用失败或输出校验失败 |
| `abandoned` | 提问已受理但始终没有任何连接订阅事件流，超时后由恢复扫描判定"未能开始执行"；文案为「该提问未能开始执行，请重新发送。」 |
| `execution_failed` | 取数/计算执行失败 |

`interaction.cancelled`（终态）：

```text
id: 5
event: interaction.cancelled
data: {"interaction_id":"it_xxx","reason":"user_requested"}
```

### 5.6 完整成功序列示例（老板查全厂订单进度）

```text
id: 1
event: interaction.started
data: {"interaction_id":"it_xxx","session_id":"sess_xxx","state":"parsing","stage":"接收","status":"accepted"}

id: 2
event: interaction.progress
data: {"state":"parsing","reason":"parse_started","stage":"解析中","status":"running","duration_ms":12}

id: 3
event: interaction.thinking
data: {"text":"正在理解您的问题，确认要查询的内容。","seq":1,"done":false,"elapsed_ms":15}

id: 4
event: interaction.progress
data: {"state":"parsing","reason":"authorize_started","stage":"权限检查中","status":"running","duration_ms":1180}

id: 5
event: interaction.thinking
data: {"text":"\n正在核对权限与可查询范围。","seq":2,"done":false,"elapsed_ms":1200}

id: 6
event: interaction.progress
data: {"state":"parsing","reason":"scope_resolution_started","stage":"核对数据范围","status":"running","duration_ms":1590}

id: 7
event: interaction.thinking
data: {"text":"\n本次查询：全厂订单进度。","seq":3,"done":false,"elapsed_ms":1620}

id: 8
event: interaction.thinking
data: {"text":"\n时间范围：2026-09-01 至 2026-09-18。","seq":4,"done":false,"elapsed_ms":1630}

id: 9
event: interaction.thinking
data: {"text":"\n正在向工厂系统取数，数据量大时会逐页取回。","seq":5,"done":false,"elapsed_ms":1650}

id: 10
event: interaction.phase
data: {"state":"authorizing","reason":"intent_complete","stage":"鉴权","status":"ok","duration_ms":1880}

id: 11
event: interaction.phase
data: {"state":"executing","reason":"authorized","stage":"取数","status":"ok","duration_ms":1895}

id: 12
event: interaction.thinking
data: {"text":"\n正在逐页取回数据：已取回 2,000 行，共 12,000 行（第 1 页）","seq":6,"done":false,"elapsed_ms":2600}

id: 13
event: interaction.thinking
data: {"text":"\n正在逐页取回数据：已取回 8,000 行，共 12,000 行（第 3 页）","seq":7,"done":false,"elapsed_ms":3200}

id: 14
event: interaction.heartbeat
data: {"interaction_id":"it_xxx"}

id: 15
event: interaction.phase
data: {"state":"composing","reason":"execution_complete","stage":"计算","status":"ok","duration_ms":3600}

id: 16
event: interaction.thinking
data: {"text":"\n正在汇总本次结果并生成答复。","seq":8,"done":false,"elapsed_ms":3620}

id: 17
event: interaction.thinking
data: {"text":"","seq":9,"done":true,"elapsed_ms":3650}

id: 18
event: interaction.result
data: {"capability_id":"fr009_factory_order_overview","columns":["order_code","plan_code","plan_missing","order_count","sheet_count","package_count","issued_qty","received_qty","progress_ratio","progress_label","worktype_count","finished_sheet_count","unfinished_sheet_count","unavailable_sheet_count","finish_state"],"row_count":12,"incomplete":false,"incomplete_reason":null,"artifact_id":"art_xxx"}

id: 19
event: interaction.completed
data: {"interaction_id":"it_xxx","status":"completed"}
```

顺序要点：`interaction.progress` 永远出现在它所描述的耗时工作**之前**，
`interaction.phase` 出现在该阶段**结束**时；两者成对理解，不要把 `progress` 当成阶段完成。
`interaction.thinking` 分片穿插在这两段等待中，**全部**早于 `interaction.result`（最后一片
`done:true` 之后不再有本轮分片）；`seq` 只在本轮内递增，与帧的 `id:` 不是同一个计数器
（例：`seq=9` 对应 `id: 17`）。`interaction.heartbeat` 可出现在任意位置，前端忽略即可。
结果帧为元数据节选；真实结果还包含汇总卡片、订单分组视图和时间窗口，字段见 §5.3.2。

## 6. 错误码约定

### 6.1 HTTP 状态码

| 状态 | 场景 | `detail` 示例 |
| --- | --- | --- |
| `400` | 请求错误：文本为空/超长、参数非法；会话数达上限（§4.14）；`exclude_kinds` 取值非法（§4.15） | `interaction text is not acceptable` / `conversation limit reached` / `unknown message kind: phase_typo` |
| `422` | 请求体结构校验失败：`drill` 只给了 `time_range_start` 而未给 `time_range_end` 等**成对字段只给一半** | `time_range_start and time_range_end must be sent together` |
| `401` | 凭据缺失、被拒绝或非法；降级模式下身份头缺失/非法 | `credential header is missing` / `credential was rejected` / `credential is invalid` |
| `403` | 身份解析拒绝（租户/用户不存在或不可用等） | `unauthenticated` / `not_found` / `forbidden` / `invalid_request` / `internal_error` |
| `404` | interaction / conversation / artifact / history / favorite 不存在，**或属于其他身份**（两者刻意不可区分） | `interaction not found` / `conversation not found` / `artifact not found` / `not found` |
| `502` | token 网关不可用，凭据换取失败 | `token exchange is unavailable` |
| `503` | 依赖服务未配置或下载失败 | `session service is not configured` / `artifact download failed` |

要点：

- **能力级"权限不足"不是 403**。HTTP 403 只出现在身份层拒绝；用户问了角色矩阵之外的
  能力时，请求照常受理（201），在事件流中以 `interaction.failed`（`error_category` =
  `forbidden`）+ 友好文案收尾，文案会告知当前可查范围（§5.5），前端直接展示该文案即可。
- 404 一律不暴露"存在但无权"的信息，前端按"不存在"处理。
- 权限校验在任何业务取数之前完成：被拒绝的轮次不会产生任何业务数据调用。

### 6.2 超时与重连约定

- SSE 断开**不等于**任务取消：后端执行是持久化且幂等单跑的，重连后通过
  `Last-Event-ID` 续传即可拿到完整事件序列；只有用户明确点击停止才调用 cancel（§4.4）。
- 建议前端为 SSE 设置读超时（大于服务端心跳间隔 15 秒，如 30–45 秒），超时即重连。
- 问答整体耗时取决于取数与计算，典型为数秒级；阶段进行中以 `interaction.progress` 驱动、
  阶段完成以 `interaction.phase` 驱动（§5.2）。前端不要按固定超时掐断，以终态事件为准，
  也无需为进度事件另设超时。
- `401` 出现时引导用户重新进入（凭据由宿主系统刷新）；`502` 可提示稍后重试。
- 时间范围约束：查询上限为**近一年**（366 天），超范围请求不会取数，直接以友好提示
  终止（§5.5 `time_range_exceeds_limit`）。**下钻回传的窗口（`drill.time_range_start/end`）同样受此约束**
  （起止倒置 / 超上限 / 落在未来一律拒绝，以 `drill_invalid_time` 终结），因此回传时应原样转发服务端
  给出的窗口，不要在前端做日期换算或"顺手修正"。

## 7. 前端实现注意事项

- 请求体永远只有业务内容（问答只有 `text`）；任何身份、租户、范围字段都不要放进
  body 或 query——放了也会被忽略。
- `session_id` 建议由 `POST /v1/conversations` 创建取得（§4.14）；也可由前端自行生成并在首轮
  提问时由服务端自动建档。刷新恢复走会话列表 + 会话详情（§4.13 / §4.15），
  **不再依赖本地存储**；本地存储仅用于记住「上次打开的是哪个会话」（§3.1）。
- SSE 的 `id` 必须持久记录到前端状态，作为重连的 `Last-Event-ID`。
- 按 `kind` 分组件渲染消息；`error` 类消息的 `text` 是可直接展示的友好文案（含权限
  不足时的可查范围说明），不要替换成通用错误话术。
- `interaction.completed` 的 `status` 区分 `"completed"` 与 `"clarifying"` 两种收尾，
  后者要把追问消息渲染为对话气泡并等待用户补充。
- `incomplete == true` 时必须向用户明示结果不完整，不能当完整结果展示。
- 异常高亮（「异常数据自动高亮」）依赖结果里的标记字段 + `card.table.alert_marker`：
  后端只下发标记，前端自行渲染样式。**当前没有能力下发标记字段**（原订单进度交期预警随生产计划
  接口弃用而取消，2026-09-15），待客户提供交期字段后再恢复。
- **卡片下钻的时间窗口（§4.2 / §5.3.1）**：点击卡片行下钻时，除用 `bind_from` 从该行取槽位值外，
  还要把该卡的 `card.time_range`（或同轮 `interaction.result.time_range`）原样回传为
  `drill.time_range_start` / `drill.time_range_end`，否则下钻会落到与卡片不同的窗口（退回当月）。
  两个字段必须成对发送；后端未回显该字段时（旧版本）忽略即可，**不要自行编造窗口**。
- 导出落盘保留（默认 90 天，重启后保留期内仍可下载）：点击导出时调
  `GET /v1/artifacts/{artifact_id}/download`，把响应当作文件直接下载/保存；收到 404
  （已过期/被清理）时走历史/收藏一键复问重新生成。

### 推送偏好与每日早报（已落地）

- 每日早报：默认开启、**不在偏好设置中配置**（不可关闭），内容按角色数据范围展示
  （昨日产量/工资摘要）。无任务凭据的无人值守 08:00 自动推送属于待客户确认的开放决策；
  当前以**用户按需生成**形态交付：`GET /v1/push/morning-report`（§4.12），用调用者本人
  凭据实时取数并按角色范围生成 + 经本地通道记录投递（真实推送通道待客户确认后接入）。
- 推送偏好：用户可配置月度/周度推送的日期、时间与内容项（工资明细推送、订单进度汇总、
  货号产量排名、完工进度总览、生产完工进度、本周产量汇总）；内容项按角色数据范围过滤。
  接口：`GET /v1/push/preferences`（含 `morning_report_enabled: true` 固定标记）、
  `PUT /v1/push/preferences`、`GET /v1/push/preferences/options`（§4.11）。

## 附录 B：前端集成约定
- 快捷问题、能力可用性、数据范围都由角色决定且服务端权威：前端不要本地硬编码角色-功能
  映射来做放行判断，直接消费 `GET /v1/quick-questions` 与拒绝文案即可。当前角色与数据范围
  用 `GET /v1/me`（§4.16）取得，**只传加密 `app_key` 即可**；角色码由服务端给出，
  前端不推断、不落库、不作为请求参数回传。
- 双入口同构：PC 悬浮小助手与 App 端调用同一套接口；App 端下载保存到本地，
  可按需启用语音输入后仍以 `text` 提交。
- 大字模式等无障碍适配为纯前端能力，接口无差异。

## 附录 A：四角色能力与数据范围矩阵

能力-角色矩阵的权威来源为 `src/factory_agent/application/permission_matrix.py`，
与 `docs/product/需求及方案整理.md` 功能表一一对应。

| 能力 | 功能 | 员工 00 | 组长 01 | 管理 02 | 老板 99 |
| --- | --- | :-: | :-: | :-: | :-: |
| FR-001 | 个人产量统计 | ✓ | ✓ | ✓ | ✓ |
| FR-002 | 个人工资（当日/当月汇总） | ✓ | ✓ | ✓ | ✓ |
| FR-003 | 个人工资明细 | ✓ | ✓ | ✓ | ✓ |
| FR-004 | 收入排名（组内名次） | — | ✓ | ✓ | ✓ |
| FR-005 | 订单/款号进度查询 | — | ✓ | ✓ | ✓ |
| FR-006 | 订单/款号产量查询 | — | ✓ | ✓ | ✓ |
| FR-007 | 小组/车间产量对比 | — | ✓ | ✓ | ✓ |
| FR-008 | 员工工资清单 | — | ✓ | ✓ | ✓ |
| FR-009 | 各订单进度（全厂订单进度总览） | — | — | — | ✓ |
| FR-010 | 车间产量总览 | — | ✓ | ✓ | ✓ |
| FR-011 | 全厂工资统计 | — | — | — | ✓ |
| FR-012 | 员工工资查询（任一员工） | — | — | — | ✓ |

说明：FR-001~FR-003 为个人能力（本人维度），四角色均可用；FR-004 收入排名（组内名次）
仅组长/管理/老板——其唯一数据源是员工工资排名接口，该接口对 00 员工返回 `code=-403`
（无权限查看工资排名），故员工角色问「我在小组里排第几」将得到 §5.5 的友好拒绝文案；
FR-005~FR-008 为管理能力，组长/管理可用；FR-010 车间产量总览对组长/管理/老板开放，返回范围由 MES
按角色行级过滤收窄（组长=本组、管理=绑定的部门/车间、老板=全厂），我方不做本地部门切分；
FR-009/FR-011/FR-013 为全厂能力，仅老板；FR-012 面向 01/02/99，目标员工必须在可查范围内。
**老板拥有所有能力**：其数据范围是下级角色的超集，因此 FR-005~FR-008 同样对老板开放
（老板问某款号/某订单/某小组的数据不应被拒）；反向不成立，01/02 不因老板能力而获得
FR-009/FR-011/FR-013。矩阵外的请求以友好拒绝处理（§5.5）。
闲聊（保留能力 `chitchat`）不属于业务矩阵：四角色均可用、不占用追问轮次、不进查询历史/
收藏。

各角色数据范围（权限不足友好提示中引用的文案，`ROLE_DATA_RANGE`）：

| 角色 | 可查范围文案 |
| --- | --- |
| 员工 | 本人的产量与工资数据 |
| 组长 | 本人数据及所绑定小组的生产与工资数据 |
| 管理 | 本人数据及所绑定车间/部门的生产与工资数据 |
| 老板 | 本人数据及全厂的订单、产量与工资数据 |

各角色快捷问题（`GET /v1/quick-questions` 按角色返回，当前员工 3 条、组长/管理/老板各 4 条）：

| 角色 | 快捷问题（对应能力） |
| --- | --- |
| 员工 | 我这个月的个人产量是多少？（FR-001）/ 我这个月的工资汇总（FR-002）/ 我这个月的工资明细是怎么算的？（FR-003） |
| 组长 | 我这个月的工资汇总（FR-002）/ 这个订单现在做到哪道工序了？（FR-005）/ 这个款这周做了多少产量？（FR-006）/ 这个月我们组每人工资清单（FR-008） |
| 管理 | 这个订单现在做到哪道工序了？（FR-005）/ 这个款这周做了多少产量？（FR-006）/ 各小组这个月产量对比一下（FR-007）/ 这个月我们车间每人工资清单（FR-008） |
| 老板 | 所有订单现在进度怎么样？（FR-009）/ 全厂各车间这个月产量情况（FR-010）/ 这个月整个厂工资发多少？（FR-011）/ 我这个月的工资汇总（FR-002） |
