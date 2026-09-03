# AI 助手前端对接 API 文档

本文档面向工厂 MES 智能问答助手的前端接入（PC 端悬浮小助手 + App 端双入口），描述
`factory-agent` 对前端提供的全部 HTTP 接口：问答与 SSE 流式返回、历史消息、快捷问题、
历史/收藏/一键复问、导出下载与健康检查。

文档分界：本文档只描述智能体对前端的 API。智能体调用客户 MES 的接口契约见
`docs/product/AI问答对外接口-整理.md`；运营、租户与计费相关接口见
`usage-admin/docs/API.md`。前端不直接调用后两者。

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
  `99` 老板。角色决定可用能力与快捷问题（见附录 A）。
- 统一错误响应：HTTP 错误一律为 `{"detail": "..."}`（请求体 schema 校验失败时为 FastAPI
  标准 `{"detail": [...]}` 结构）。业务过程中的权限不足、追问、失败**不是 HTTP 错误**，
  而是 SSE 事件流内的 `interaction.failed` / `interaction.clarification` 事件 +
  持久化消息，见第 5 节。
- 时间：服务端时区 `Asia/Shanghai`；时间字段为 ISO 8601 字符串。
- 分页：列表接口使用 `limit` + `cursor` 查询参数，响应携带 `next_cursor`（为 `null`
  表示没有下一页）。
- 排障：可选携带 `X-Request-ID` 请求头，服务端会原样回传该头，便于与后端日志对账。

## 2. 推荐前端流程

1. 打开助手面板时，调用 `GET /v1/quick-questions` 拉取当前角色的快捷问题按钮；可选调用
   `GET /v1/favorites` 展示收藏入口。
2. 用户提问（或点击快捷问题）时，调用
   `POST /v1/sessions/{session_id}/interactions`，请求体只有 `text` 一个字段，返回 201
   与 `interaction_id`。`session_id` 由前端生成并维护（见 §3.1）。
3. 用返回的 `interaction_id` 调用 `GET /v1/interactions/{interaction_id}/stream`
   订阅 SSE 事件流，按 §5 渲染阶段、追问与结果。
4. 收到终态事件（`interaction.completed` / `interaction.failed` /
   `interaction.cancelled`）后结束本轮加载态。
5. 收到 `interaction.result` 且 `artifact_id` 非空时，展示"导出报表"入口；用户点击后调用
   `GET /v1/artifacts/{artifact_id}/download`，把响应作为文件直接下载/保存（不留存，
   无需预签名 URL）。
6. SSE 意外断开时，用 `Last-Event-ID` 重连断点续传（§5.1），**不要**重新发起提问。
7. 刷新页面 / 重新打开面板后，用 `GET /v1/sessions/{session_id}/messages` 恢复聊天内容；
   跨会话的历史查询入口用 `GET /v1/history`。
8. 收藏的一键复问：`POST /v1/favorites/{favorite_id}/re-ask` 取回保存的查询意图，由前端
   转成自然语言问题后回到第 2 步重新执行。

## 3. 核心对象

### 3.1 Session

一次多轮对话。**服务端没有会话创建、会话列表、会话快照接口**：会话经 interaction 建档，
`session_id` 是前端生成并传入的路径参数（非空字符串即可，建议 UUID），服务端按
`(租户, 用户)` 归属记录其下的 interaction 与消息。

- 前端应自行保存使用过的 `session_id`（如本地存储），用于刷新后调用历史消息接口恢复。
- 会话上下文恢复路径：`GET /v1/sessions/{session_id}/messages`（已知 session_id 时）+
  `GET /v1/history`（按用户维度的查询历史）。

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
前端可将 `state` 与阶段文案的映射用于进度展示，阶段变化以
`interaction.phase` 事件为准。

### 3.3 Message

持久化消息，`GET /v1/sessions/{session_id}/messages` 返回的视图为：

```json
{
  "message_id": "msg_xxx",
  "role": "assistant",
  "kind": "result_table",
  "sequence": 6,
  "text": "已返回 12 行结果。"
}
```

`role`：`user` / `assistant` / `system`

`kind`：

| 值 | 含义与建议渲染 |
| --- | --- |
| `plain_text` | 普通文本（用户提问） | 普通气泡 |
| `clarification` | 助手追问（缺时间、款号等条件时） | 追问气泡，引导用户补充 |
| `phase` | 阶段变化记录 | 可折叠的过程信息 |
| `result_table` | 结果卡片消息，`text` 为结果摘要（如"已返回 N 行结果。"） | 结果卡片 |
| `error` | 错误/拒绝/取消提示，`text` 为可直接展示的友好文案 | 错误提示气泡 |

`sequence` 为该消息在轮内的单调递增序号，与 SSE 的 `id:` 同源。

### 3.4 Artifact（导出产物）

结果导出采用**即时生成、直接下载、服务端不留存**策略：导出文件在内存中即时渲染，
只短暂保留在进程内缓冲（约 15 分钟窗口），服务端**不落盘、不入对象存储、无生命周期**；
"回头再取"通过历史记录/收藏一键复问**重新执行 → 直接下载**，留查询不留文件。
`interaction.result` 携带的 `artifact_id` 是一次导出的短时效凭证：

```json
{ "artifact_id": "art_xxx" }
```

前端拿到 `artifact_id` 后调用 `GET /v1/artifacts/{artifact_id}/download`，响应即文件
本体（`Content-Disposition: attachment`），浏览器直接下载、App 端保存到本地；不再需要
预签名链接。链接过期或文件不可得（返回 404）时，引导用户用历史/收藏一键复问重新生成。

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
- 请求体**只有** `text`；不要携带任何身份、租户、范围字段。
- `session_id` 由前端生成，首轮即视为建档该会话。

响应 `201`：

```json
{
  "interaction_id": "it_xxx",
  "session_id": "sess_xxx",
  "status": "pending",
  "state": "parsing"
}
```

错误：`400`（文本为空/超长）、`401`（凭据缺失/无效）、`403`（身份解析拒绝）、
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
      "role": "user",
      "kind": "plain_text",
      "sequence": 1,
      "text": "我这个月的工资是多少？"
    },
    {
      "message_id": "msg_2",
      "role": "assistant",
      "kind": "result_table",
      "sequence": 6,
      "text": "已返回 1 行结果。"
    }
  ],
  "next_cursor": null
}
```

- 消息按 `(interaction, sequence)` 归属过滤：只能看到当前身份自己的会话消息；
  访问他人会话返回 403。

### 4.6 角色化快捷问题

`GET /v1/quick-questions`

响应：`QuickQuestion[]`，**按当前角色动态返回**（员工/组长/管理/老板各返回 4–6 条）：

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

### 4.10 导出下载

`GET /v1/artifacts/{artifact_id}/download`

响应即文件本体（流式返回，`Content-Disposition: attachment`），前端/浏览器直接把响应当
文件下载；App 端保存到本地。文件名按"角色_功能_时间范围_生成时间"约定。

- `artifact_id` 来自 `interaction.result` 事件（§5.3），是一次导出的短时效凭证。
- 导出**即时生成、直接下载、服务端不留存**：XLSX 在内存渲染后仅短暂保留于进程内缓冲
  （约 15 分钟窗口），无对象存储、无预签名链接、无留存生命周期；缓冲过期或文件不可得
  时接口返回 404，应引导用户用历史/收藏一键复问**重新执行 → 直接下载**。
- 下载会重新校验凭据与归属：他人 `artifact_id`、已过期的与不存在的 `artifact_id` 一律
  返回 404（刻意不可区分）。


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
| `interaction.phase` | 阶段推进 | 否 |
| `interaction.clarification` | 追问补全参数 | 否 |
| `interaction.result` | 结果卡片数据 | 否 |
| `interaction.heartbeat` | 保活 | 否 |
| `interaction.completed` | 本轮正常结束（结果就绪或进入追问） | 是 |
| `interaction.failed` | 本轮失败（含权限不足友好拒绝） | 是 |
| `interaction.cancelled` | 本轮被用户取消 | 是 |

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
`composing`（计算）。

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
data: {"capability_id":"fr009_factory_order_overview","columns":["order_code","huohao","customer_name","plan_qty","completed_qty","progress_ratio","delivery_warning","days_remaining"],"row_count":12,"incomplete":false,"incomplete_reason":null,"artifact_id":"art_xxx"}
```

字段：

| 字段 | 说明 |
| --- | --- |
| `capability_id` | 本次执行的能力（recipe 形式 id，与附录 A 的 FR 编号一一对应，如 `fr009_factory_order_overview` ↔ FR-009） |
| `columns` | 结果列名列表（字符串数组），列序即报表列序 |
| `row_count` | 结果行数 |
| `incomplete` | 结果是否不完整（如分页拉取异常、个别指标不可用）；为 `true` 时必须向用户展示不完整提示 |
| `incomplete_reason` | 不完整原因（如 `pagination_*`、`metric_unavailable:*`、`reconciliation_failed`），`null` 表示完整 |
| `artifact_id` | 导出产物 ID；为 `null` 表示本次未生成导出，不展示导出按钮 |

事件本身携带的是卡片元数据（列定义、行数、完整性、导出入口）；行级明细数据在导出文件
中。持久化的 `result_table` 消息（§3.3）是该结果的历史形态，`text` 为结果摘要
（如"已返回 12 行结果。"）。

**交期预警标记（异常数据自动高亮）**：老板"各订单进度"（FR-009，全厂订单进度总览）的
输出列中包含两个标记字段：

| 列名 | 取值 | 含义 |
| --- | --- | --- |
| `delivery_warning` | `'1'` / `'0'`（字符串布尔） | 交期预警：订单未完工且距交期剩余天数 ≤ 预警阈值（默认阈值 = max(1, ⌈总工期 × 10%⌉)；缺开始/交期日期时回退固定 7 天窗口） |
| `days_remaining` | 剩余天数字符串 | 距交期剩余天数，负数表示已逾期 |

前端对 `delivery_warning == '1'` 的行做标红等"异常数据自动高亮"处理，并可展示
`days_remaining` 辅助说明。**后端只输出标记字段，不输出任何 UI 样式**。

`interaction.completed`（终态）：

```text
id: 6
event: interaction.completed
data: {"interaction_id":"it_xxx","status":"completed"}
```

注意：`data.status` 有两种取值——`"completed"`（结果就绪）与 `"clarifying"`
（本轮以追问收尾，等待用户补充后发起新一轮）。

### 5.4 失败与取消事件

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
| `forbidden_*` | 执行层范围规则拒绝（如工资明细空参查全部仅限老板） |
| `filter_*` | 订单号/款号/小组等业务条件解析失败或超出可查范围 |
| `time_range_missing` | 缺少时间条件且无法追问补全 |
| `time_range_exceeds_limit` | 时间范围超过近一年上限（友好终止，不发起取数） |
| `clarification_exhausted` | 追问轮次用尽 |
| `capability_unresolved` / `capability_unregistered` | 意图无法落到已注册能力 |
| `gateway_*` / `model_output_invalid` | 模型调用失败或输出校验失败 |
| `execution_failed` | 取数/计算执行失败 |

`interaction.cancelled`（终态）：

```text
id: 5
event: interaction.cancelled
data: {"interaction_id":"it_xxx","reason":"user_requested"}
```

### 5.5 完整成功序列示例（老板查全厂订单进度）

```text
id: 1
event: interaction.started
data: {"interaction_id":"it_xxx","session_id":"sess_xxx","state":"parsing","stage":"接收","status":"accepted"}

id: 2
event: interaction.phase
data: {"state":"authorizing","reason":"intent_complete","stage":"鉴权","status":"ok","duration_ms":410}

id: 3
event: interaction.phase
data: {"state":"executing","reason":"authorized","stage":"取数","status":"ok","duration_ms":425}

id: 4
event: interaction.phase
data: {"state":"composing","reason":"execution_complete","stage":"计算","status":"ok","duration_ms":2130}

id: 5
event: interaction.result
data: {"capability_id":"fr009_factory_order_overview","columns":["order_code","huohao","customer_name","plan_qty","completed_qty","progress_ratio","delivery_warning","days_remaining"],"row_count":12,"incomplete":false,"incomplete_reason":null,"artifact_id":"art_xxx"}

id: 6
event: interaction.completed
data: {"interaction_id":"it_xxx","status":"completed"}
```

## 6. 错误码约定

### 6.1 HTTP 状态码

| 状态 | 场景 | `detail` 示例 |
| --- | --- | --- |
| `400` | 请求错误：文本为空/超长、参数非法 | `interaction text is not acceptable` |
| `401` | 凭据缺失、被拒绝或非法；降级模式下身份头缺失/非法 | `credential header is missing` / `credential was rejected` / `credential is invalid` |
| `403` | 身份解析拒绝（租户/用户不存在或不可用等） | `unauthenticated` / `not_found` / `forbidden` / `invalid_request` / `internal_error` |
| `404` | interaction / artifact / history / favorite 不存在，**或属于其他身份**（两者刻意不可区分） | `interaction not found` / `artifact not found` / `not found` |
| `502` | token 网关不可用，凭据换取失败 | `token exchange is unavailable` |
| `503` | 依赖服务未配置或下载失败 | `session service is not configured` / `artifact download failed` |

要点：

- **能力级"权限不足"不是 403**。HTTP 403 只出现在身份层拒绝；用户问了角色矩阵之外的
  能力时，请求照常受理（201），在事件流中以 `interaction.failed`（`error_category` =
  `forbidden`）+ 友好文案收尾，文案会告知当前可查范围（§5.4），前端直接展示该文案即可。
- 404 一律不暴露"存在但无权"的信息，前端按"不存在"处理。
- 权限校验在任何业务取数之前完成：被拒绝的轮次不会产生任何业务数据调用。

### 6.2 超时与重连约定

- SSE 断开**不等于**任务取消：后端执行是持久化且幂等单跑的，重连后通过
  `Last-Event-ID` 续传即可拿到完整事件序列；只有用户明确点击停止才调用 cancel（§4.4）。
- 建议前端为 SSE 设置读超时（大于服务端心跳间隔 15 秒，如 30–45 秒），超时即重连。
- 问答整体耗时取决于取数与计算，典型为数秒级；阶段以 `interaction.phase` 事件驱动，
  前端不要按固定超时掐断，以终态事件为准。
- `401` 出现时引导用户重新进入（凭据由宿主系统刷新）；`502` 可提示稍后重试。
- 时间范围约束：查询上限为**近一年**（366 天），超范围请求不会取数，直接以友好提示
  终止（§5.4 `time_range_exceeds_limit`）。

## 7. 前端实现注意事项

- 请求体永远只有业务内容（问答只有 `text`）；任何身份、租户、范围字段都不要放进
  body 或 query——放了也会被忽略。
- `session_id` 前端生成并自行保存；服务端不提供会话列表/快照接口，刷新恢复依赖
  messages + history（§3.1）。
- SSE 的 `id` 必须持久记录到前端状态，作为重连的 `Last-Event-ID`。
- 按 `kind` 分组件渲染消息；`error` 类消息的 `text` 是可直接展示的友好文案（含权限
  不足时的可查范围说明），不要替换成通用错误话术。
- `interaction.completed` 的 `status` 区分 `"completed"` 与 `"clarifying"` 两种收尾，
  后者要把追问消息渲染为对话气泡并等待用户补充。
- `incomplete == true` 时必须向用户明示结果不完整，不能当完整结果展示。
- 交期预警等异常高亮由前端依据标记字段（`delivery_warning` / `days_remaining`）自行
  渲染，后端不下发任何样式。
- 导出链接短时效、不留存：点击导出时再调 §4.10 取链接并立即下载，不要把
  `url` 存入历史；需要"回头再取"时走一键复问重新生成。
- 快捷问题、能力可用性、数据范围都由角色决定且服务端权威：前端不要本地硬编码角色-功能
  映射来做放行判断，直接消费 `GET /v1/quick-questions` 与拒绝文案即可。
- 双入口同构：PC 悬浮小助手与 App 端调用同一套接口；App 端下载保存到本地，
  可按需启用语音输入后仍以 `text` 提交。
- 大字模式等无障碍适配为纯前端能力，接口无差异。

### 推送与早报（规划中，暂无接口）

- 每日早报：系统默认推送，每天早 8 点推送昨日产量/工资摘要，内容按角色数据范围展示；
  **不在偏好设置中配置**（默认开启、不可关）。
- 推送偏好：用户可自行配置月度/周度推送的日期、时间与内容项（工资明细推送、订单进度
  汇总、货号产量排名、完工进度总览、生产完工进度、本周产量汇总等）；推送项按角色数据
  范围展示。
- 上述能力尚未提供 API，接口形态确定后在本文件补充；前端当前无需实现对接。

## 附录 A：四角色能力与数据范围矩阵

能力-角色矩阵的权威来源为 `src/factory_agent/application/permission_matrix.py`，
与 `docs/product/需求及方案整理.md` 功能表一一对应。

| 能力 | 功能 | 员工 00 | 组长 01 | 管理 02 | 老板 99 |
| --- | --- | :-: | :-: | :-: | :-: |
| FR-001 | 个人产量统计 | ✓ | ✓ | ✓ | ✓ |
| FR-002 | 个人工资（当日/当月汇总） | ✓ | ✓ | ✓ | ✓ |
| FR-003 | 个人工资明细 | ✓ | ✓ | ✓ | ✓ |
| FR-004 | 收入排名（组内名次） | ✓ | ✓ | ✓ | ✓ |
| FR-005 | 订单/款号进度查询 | — | ✓ | ✓ | — |
| FR-006 | 订单/款号产量查询 | — | ✓ | ✓ | — |
| FR-007 | 小组/车间产量对比 | — | ✓ | ✓ | — |
| FR-008 | 员工工资清单 | — | ✓ | ✓ | — |
| FR-009 | 各订单进度（全厂订单进度总览，含交期预警列） | — | — | — | ✓ |
| FR-010 | 车间产量总览 | — | — | — | ✓ |
| FR-011 | 全厂工资统计 | — | — | — | ✓ |
| FR-012 | 员工工资查询（任一员工） | — | — | — | ✓ |

说明：FR-001~FR-004 为个人能力（本人维度），四角色均可用；FR-005~FR-008 为管理能力，
限组长/管理；FR-009~FR-012 为全厂能力，仅老板。矩阵外的请求以友好拒绝处理（§5.4）。

各角色数据范围（权限不足友好提示中引用的文案，`ROLE_DATA_RANGE`）：

| 角色 | 可查范围文案 |
| --- | --- |
| 员工 | 本人的产量与工资数据 |
| 组长 | 本人数据及所绑定小组的生产与工资数据 |
| 管理 | 本人数据及所绑定车间/部门的生产与工资数据 |
| 老板 | 本人数据及全厂的订单、产量与工资数据 |

各角色快捷问题（`GET /v1/quick-questions` 按角色返回，当前每角色 4 条）：

| 角色 | 快捷问题（对应能力） |
| --- | --- |
| 员工 | 我这个月的个人产量是多少？（FR-001）/ 我这个月的工资汇总（FR-002）/ 我这个月的工资明细是怎么算的？（FR-003）/ 我在小组里排第几？（FR-004） |
| 组长 | 我这个月的工资汇总（FR-002）/ 这个订单现在做到哪道工序了？（FR-005）/ 这个款这周做了多少产量？（FR-006）/ 这个月我们组每人工资清单（FR-008） |
| 管理 | 这个订单现在做到哪道工序了？（FR-005）/ 这个款这周做了多少产量？（FR-006）/ 各小组这个月产量对比一下（FR-007）/ 这个月我们车间每人工资清单（FR-008） |
| 老板 | 所有订单现在进度怎么样？（FR-009）/ 整个车间这个月产量情况（FR-010）/ 这个月整个厂工资发多少？（FR-011）/ 我这个月的工资汇总（FR-002） |
