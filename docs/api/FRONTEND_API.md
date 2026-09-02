# FlyReport 前端 API 文档（精简版）

本文档面向前端调试台和业务页面接入，覆盖本次已约定并实现的 FlyReport 多轮对话、SSE 流式返回、历史消息、交互状态和报告产物接口。

## 1. 基本约定

- API 前缀：`/v1/fly-reports`
- 本地默认后端：`http://127.0.0.1:8000`
- 所有会话和消息接口都需要传 `user_id`；会话创建还需要 `tenant_id`。
- 前端不传 `output_format`。默认生成 `docx`；如需 `markdown` 或 `pdf`，由用户在 query 文本中表达，例如“导出 markdown”。
- SSE 只负责当前连接的实时推送；历史展示以 `GET /sessions/{session_id}/messages` 为准。

## 2. 推荐前端流程

1. 调用 `POST /sessions` 创建会话，保存 `session_id`。
2. 调用 `POST /sessions/{session_id}/messages/stream` 发送用户输入并消费 SSE。
3. SSE 中用 `message.delta` 展示临时流式文本，用 `message.item` 展示可持久化消息卡片。
4. 收到 `interaction.completed`、`interaction.failed` 或 `interaction.cancelled` 后结束当前轮加载态。
5. 调用 `GET /sessions/{session_id}/messages` 刷新历史消息。
6. 如有报告产物，调用 `GET /sessions/{session_id}/artifacts` 获取产物列表并展示下载入口。

## 3. 核心对象

### Session

一次长期多轮对话。

关键字段：

```json
{
  "session_id": "sess_xxx",
  "state": "previewing",
  "title": "飞行周报",
  "last_user_text": "生成飞行周报，导出 markdown",
  "turn_count": 2,
  "created_at": "2026-04-28T10:00:00+08:00",
  "updated_at": "2026-04-28T10:01:00+08:00"
}
```

### Interaction

一次用户输入对应的一轮后端处理。普通问答和报告生成都走 interaction。

`status`：

- `pending`
- `streaming`
- `completed`
- `failed`
- `cancelled`

`phase`：

- `intake`
- `parsing`
- `clarifying`
- `authorizing`
- `fetching`
- `analyzing`
- `previewing`
- `rendering`
- `delivering`
- `done`

### Message

前端最终展示和历史回看的消息。

`role`：`user`、`assistant`、`system`

`type`：

- `plain_text`：普通文本
- `phase`：阶段变化
- `todo`：步骤列表
- `artifact`：产物卡片
- `summary`：最终总结
- `error`：错误提示

`status`：`pending`、`running`、`completed`、`failed`、`cancelled`

## 4. 接口清单

## 4.1 健康检查

`GET /health`

响应：

```json
{
  "status": "healthy"
}
```

## 4.2 创建会话

`POST /v1/fly-reports/sessions`

请求：

```json
{
  "tenant_id": "e2e-tenant",
  "user_id": "e2e-user",
  "initial_query": null
}
```

响应：

```json
{
  "session_id": "sess_xxx",
  "status": "created",
  "links": {
    "session": "/v1/fly-reports/sessions/sess_xxx",
    "messages": "/v1/fly-reports/sessions/sess_xxx/messages",
    "stream": "/v1/fly-reports/sessions/sess_xxx/messages/stream"
  }
}
```

说明：

- 前端建议先创建 session，再单独调用流式消息接口。
- `initial_query` 目前可选；调试台默认不传。

## 4.3 发送流式消息

`POST /v1/fly-reports/sessions/{session_id}/messages/stream`

请求：

```json
{
  "user_id": "e2e-user",
  "text": "生成飞行周报，导出 markdown",
  "template_ref": null,
  "metadata": {
    "client_message_id": "client-msg-001"
  }
}
```

说明：

- 前端只发送用户原始文本。
- 不要额外传 `output_format`；后端会从文本中识别格式需求。
- 如果文本没有表达格式，后端默认生成 `docx`。

响应类型：`text/event-stream`

响应头：

```http
Content-Type: text/event-stream
Cache-Control: no-cache
Connection: keep-alive
X-Accel-Buffering: no
```

### SSE 事件：interaction.started

每轮流式响应的第一个事件。

```text
event: interaction.started
data: {"interaction_id":"it_xxx","session_id":"sess_xxx","status":"pending","phase":"intake","message_count":1,"artifact_count":0}

```

前端处理：

- 保存 `interaction_id`。
- 当前输入进入运行态。

### SSE 事件：message.delta

普通问答的临时文本增量。它用于实时展示，不作为历史消息最终形态。

```text
event: message.delta
data: {"message_id":"msg_xxx","interaction_id":"it_xxx","text":"已收到你的问题"}

```

前端处理：

- 按 `message_id` 拼接 `text`。
- 显示为一个临时 assistant 气泡。
- 后续收到同 `message_id` 的 `message.item` 后，用最终消息覆盖临时气泡。

### SSE 事件：message.item

可持久化消息。历史接口返回的也是这类结构。

通用结构：

```json
{
  "message_id": "msg_xxx",
  "interaction_id": "it_xxx",
  "role": "assistant",
  "type": "phase",
  "title": "阶段更新",
  "text": "正在解析你的报告需求。",
  "status": "running",
  "created_at": "2026-04-28T10:00:00+08:00",
  "data": {},
  "actions": [],
  "meta": {}
}
```

#### phase 示例

```text
event: message.item
data: {"message_id":"msg_1","interaction_id":"it_xxx","role":"assistant","type":"phase","title":"阶段更新","text":"正在生成报告文件。","status":"running","data":{"phase":"rendering","label":"生成文件"},"actions":[],"meta":{"source":"fly_report","phase":"rendering"}}

```

#### todo 示例

```text
event: message.item
data: {"message_id":"msg_2","interaction_id":"it_xxx","role":"assistant","type":"todo","title":"报告生成计划","text":"已生成报告处理计划。","status":"running","data":{"items":[{"id":"step_1","text":"解析报告时间和范围","status":"running"},{"id":"step_2","text":"获取并分析业务数据","status":"pending"},{"id":"step_3","text":"生成报告文件","status":"pending"}]},"actions":[],"meta":{"source":"fly_report","phase":"parsing"}}

```

#### artifact 示例

```text
event: message.item
data: {"message_id":"msg_3","interaction_id":"it_xxx","role":"assistant","type":"artifact","title":"报告已生成","text":"报告文件已生成。","status":"completed","data":{"artifact_id":"fly-report.md","artifact_name":"fly-report.md","content_type":"text/markdown; charset=utf-8","download_url":"/v1/fly-reports/sessions/sess_xxx/artifacts/fly-report.md?user_id=e2e-user"},"actions":[],"meta":{"source":"fly_report","phase":"delivering"}}

```

#### summary 示例

```text
event: message.item
data: {"message_id":"msg_4","interaction_id":"it_xxx","role":"assistant","type":"summary","title":"处理完成","text":"已生成报告预览，请确认后导出。","status":"completed","data":{"preview_brief":{}},"actions":[],"meta":{"source":"fly_report","phase":"done"}}

```

### SSE 事件：interaction.completed

当前轮正常完成。

```text
event: interaction.completed
data: {"interaction_id":"it_xxx","session_id":"sess_xxx","status":"completed","phase":"done","message_count":5,"artifact_count":1}

```

### SSE 事件：interaction.failed

当前轮失败。

```text
event: interaction.failed
data: {"interaction_id":"it_xxx","session_id":"sess_xxx","status":"failed","phase":"fetching","error":"upstream request failed"}

```

### SSE 事件：interaction.cancelled

当前轮被用户取消。

```text
event: interaction.cancelled
data: {"interaction_id":"it_xxx","session_id":"sess_xxx","status":"cancelled","phase":"rendering","error":"user_requested"}

```

## 4.4 获取历史消息

`GET /v1/fly-reports/sessions/{session_id}/messages`

Query：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 当前用户 ID |
| `limit` | 否 | 默认 100，最大 500 |
| `before_message_id` | 否 | 翻页游标，取更早消息 |

响应：

```json
{
  "session_id": "sess_xxx",
  "messages": [
    {
      "message_id": "msg_user",
      "interaction_id": "it_xxx",
      "role": "user",
      "type": "plain_text",
      "title": null,
      "text": "生成飞行周报，导出 markdown",
      "status": "completed",
      "created_at": "2026-04-28T10:00:00+08:00",
      "data": {},
      "actions": [],
      "meta": {}
    }
  ],
  "next_before_message_id": null
}
```

前端处理：

- 页面刷新、切换会话后优先调用该接口恢复聊天内容。
- `messages` 已按创建时间和 interaction 内顺序升序返回。

## 4.5 获取 interaction 状态

`GET /v1/fly-reports/interactions/{interaction_id}`

Query：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 当前用户 ID |

响应：

```json
{
  "interaction_id": "it_xxx",
  "session_id": "sess_xxx",
  "status": "completed",
  "phase": "done",
  "message_count": 5,
  "artifact_count": 1,
  "created_at": "2026-04-28T10:00:00+08:00",
  "started_at": "2026-04-28T10:00:00+08:00",
  "completed_at": "2026-04-28T10:00:10+08:00",
  "error": null
}
```

使用场景：

- SSE 断开后确认当前轮是否完成。
- 调试面板展示最近交互状态。

## 4.6 取消当前 interaction

`POST /v1/fly-reports/interactions/{interaction_id}/cancel`

请求：

```json
{
  "user_id": "e2e-user",
  "reason": "user_requested"
}
```

响应：

```json
{
  "interaction_id": "it_xxx",
  "session_id": "sess_xxx",
  "status": "cancelled",
  "message": "Interaction cancellation accepted"
}
```

说明：

- 只有用户主动点击停止时调用。
- 页面关闭、路由切换、SSE 断开不应自动取消后端任务。

## 4.7 获取报告产物列表

`GET /v1/fly-reports/sessions/{session_id}/artifacts`

Query：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 当前用户 ID |
| `interaction_id` | 否 | 只看某一轮生成的产物 |

响应：

```json
[
  {
    "artifact_id": "fly-report.md",
    "interaction_id": "it_xxx",
    "filename": "fly-report.md",
    "output_format": "markdown",
    "template_ref": null,
    "content_type": "text/markdown; charset=utf-8",
    "artifact_path": "/absolute/path/to/fly-report.md",
    "download_url": "/v1/fly-reports/sessions/sess_xxx/artifacts/fly-report.md?user_id=e2e-user",
    "created_at": "2026-04-28T10:00:10+08:00"
  }
]
```

## 4.8 下载报告产物

`GET /v1/fly-reports/sessions/{session_id}/artifacts/{filename}`

Query：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 当前用户 ID |

响应：文件流。

前端处理：

- 直接使用 artifact 的 `download_url`。
- 如果页面使用 Vite proxy 且 `API Base` 为空，下载链接可拼接当前 origin。

## 4.9 查询会话列表

`GET /v1/fly-reports/sessions`

Query：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `tenant_id` | 是 | 租户 ID |
| `user_id` | 是 | 用户 ID |
| `limit` | 否 | 默认 50，最大 200 |
| `keyword` | 否 | 按标题或最近用户输入搜索 |
| `state` | 否 | 按会话状态过滤 |

响应：

```json
[
  {
    "session_id": "sess_xxx",
    "state": "previewing",
    "title": "飞行周报",
    "last_user_text": "生成飞行周报，导出 markdown",
    "revision": 3,
    "created_at": "2026-04-28T10:00:00+08:00",
    "updated_at": "2026-04-28T10:01:00+08:00"
  }
]
```

## 4.10 获取会话快照

`GET /v1/fly-reports/sessions/{session_id}`

Query：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 当前用户 ID |

响应：

```json
{
  "session_id": "sess_xxx",
  "state": "previewing",
  "title": "飞行周报",
  "last_user_text": "生成飞行周报，导出 markdown",
  "turn_count": 2,
  "filter_spec": {},
  "created_at": "2026-04-28T10:00:00+08:00",
  "updated_at": "2026-04-28T10:01:00+08:00"
}
```

## 4.11 获取 HTML 预览

`GET /v1/fly-reports/sessions/{session_id}/preview`

Query：

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 当前用户 ID |

响应：`text/html`

使用场景：

- 报告已经进入可预览状态后，在 iframe 或新页面中展示草稿预览。

## 5. 错误码约定

| HTTP 状态 | 场景 |
| --- | --- |
| `400` | 输入为空、文本过长、模板参数非法等请求错误 |
| `404` | session、interaction 或 artifact 不存在，或用户无权访问 |
| `409` | 当前状态不允许执行该操作，例如重复取消、状态不可确认 |
| `500` | 服务端未预期错误 |

错误响应示例：

```json
{
  "detail": "text must be a non-empty string"
}
```

## 6. 前端实现注意事项

- 不要把 SSE 断开等同于任务取消；只有用户明确点击停止才调用 cancel。
- `message.delta` 只做实时体验，最终历史以 `message.item` 和历史接口为准。
- 报告生成可能产生多个 `phase`、`todo`、`artifact`、`summary` 消息，前端按 `type` 分组件展示即可。
- `download_url` 是后端返回的相对路径；本地调试台可用当前 origin 拼接。
- 前端刷新页面后，应使用 `session_id` 调 `messages`、`artifacts`、`session snapshot` 恢复状态。
- 当前接口没有暴露“意图类型”字段；前端不需要判断用户输入是普通问答还是报告生成，只消费返回消息。

## 7. 本次已落地的前端调试台

位置：`front/chat`

能力：

- 创建 FlyReport session。
- 发送单轮/多轮 streaming message。
- 实时展示 SSE 文本和消息卡片。
- 刷新历史消息。
- 查看 artifacts 和 interactions。
- 下载报告产物。
- 通过 Vite proxy 连接本地后端，避免 CORS 问题。

启动：

```bash
cd front/chat
npm run dev
```

访问：

```text
http://127.0.0.1:5173/
```
