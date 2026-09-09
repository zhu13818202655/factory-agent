# ADR-0007：交互执行与 SSE 连接解耦（后台执行器）

- 状态：Accepted（2026-09-09 实现；Story 依据见 `.github/story/#4.md`）
- 日期：2026-09-09
- 所有者：项目维护者
- 关联：`docs/product/AI问答对外接口-整理.md`（SSE 契约，零变更）、
  `.github/story/#4.md`（背景与故障链）、ADR-0003（计量独立事务）

## 1. 背景

一次交互的完整执行（解析 → 鉴权 → 取数 → 计算 → 落库）原本寄生在**发起它的那条 SSE
连接**里：`SessionService.stream()` 的 `claim_run` CAS 赢家在自己的响应生成器内直接驱动
管线。连接断开（前端超时、代理超时、浏览器关闭、调试断点冻结进程）→ uvicorn 取消响应
任务 → `CancelledError` 打断管线 → 结果与终态事件永不落库，交互永久停留在
`running/executing`。此故障链已在本地实证（2026-09-08，断点冻结 > 前端 600s 读超时）。

既有安全网（「最小改 + 自愈」）保留：`_follow` 超时下发 wire-only 终态
（`follow_timeout`）；stale `running` 经 `fail_stale_run` CAS 标记 `executor_lost`。

## 2. 决策

### 2.1 执行者与连接分离

- `stream()` 的 claim 赢家把管线交给 **`InteractionRunExecutor`**（同进程
  `asyncio.create_task`，不引入外部队列/独立 worker），自身立即转入 `_follow` 旁听；
  所有连接（含发起者）一律 replay + follow。
- 执行器持有 `TrustedCredential`、usage context、credential binder；逐事件 `commit`
  落库（既有 `InteractionCommit` 事务语义不变）；任务结束即结束，与任何连接状态无关。
- 旁听事件可见性：进程内 per-interaction `asyncio.Event` 通知（执行器每次 commit 后
  set），跟随连接醒来立即轮询；无通知时按 `heartbeat_seconds`（15s）静默后发心跳。
  因此正常路径下旁听延迟与原实现同量级，不是纯 15s 轮询。

### 2.2 取消语义（双通道）

- **DB 状态**（跨进程/重启权威通道）：`cancel` API 落库 `cancelled` 终态 + 终态事件 +
  计量 completion 事件，语义与既有实现完全一致。
- **进程内 `asyncio.Event`**（低延迟通道）：cancel API 在落库后 set 注册执行器的取消
  事件；执行器在**协作式停止点**观察。停止点集合：管线开始前（parse 前）、业务取数
  前（`runner.run` 前）、compose 前。
- 执行器发现已取消后**静默退出**：不再产出任何事件、不再消耗剩余 call budget；终态/
  消息/计量由 cancel API 负责，执行器绝不覆盖。

### 2.3 运行时长预算（与单调用超时分层）

- 执行器持有整体 wall-clock 预算 `session_run_timeout_seconds`（默认 300s）；在上述
  停止点检查，超时后经既有 `_fail` 路径落库 `failed/run_timeout` 终态 + 计量 completion
  事件。
- 分层：单调用超时（LLM 30s / MES 超时）管「卡住的单次调用」；预算管「活着但整体拖
  太长」；`fail_stale_run` 自愈靠 `updated_at` 停滞，抓「死了的」。
- **约束：`run_timeout_seconds` 必须小于 `stale_running_seconds`**（300 < 600），保证
  活着但慢的交互先被自己的预算终结，不会被旁听连接误判为孤儿。

### 2.4 进程重启恢复（startup sweep）

- `create_app` 的 lifespan 启动时执行一次 `sweep_stale_runs()`：批量 CAS
  （`fail_stale_interaction_runs`，去掉单条语句的 owner 条件）把所有
  `status='running' 且 updated_at 超过 stale_running_seconds` 的交互标记
  `failed/executor_lost`，并逐条补齐终态事件/系统消息/计量 completion。
- 幂等：CAS 天然幂等，重复启动、多 worker 同时启动均只标记一次（集成测试覆盖）。

### 2.5 停机语义（拍板：bounded drain，不无限等待）

- lifespan 关闭时对在跑执行器**有界 drain**（`SessionService.shutdown`，默认 10s）：
  预算内跑完的照常落库；超时未完成的直接取消，交互留在 `running`，由**下一次启动的
  sweep** 兜底标记 `executor_lost`。
- 理由：deploy 重建容器时 DB 仍可用，但 drain 不能阻塞容器停止；sweep 与 drain 组合
  的收敛时间 ≈ 部署周期 + 启动即修，满足「无永久孤儿」验收项。

### 2.6 ContextVar 复制语义（3.2.4 结论）

- `asyncio.create_task` 拷贝当前 context：执行器任务内 `set_usage_context(...)` /
  `bind_for(...)` 的 set 都落在**任务自己的 context 副本**里，不影响发起请求的
  handler context，也不影响其他并发交互。MES adapter 在 `runner.run` 内被任务 await，
  读取的是同一 context——计量归属与凭据绑定天然隔离，**无需显式传参重构**。

### 2.7 并发矩阵推演（3.1.4 结论）

| 场景 | 推演 |
|---|---|
| uvicorn 多 worker | claim CAS 保证恰好一个 worker 的执行器执行；sweep 批量 CAS 跨 worker 幂等；cancel 落库为权威通道，其他 worker 的执行器经停止点 DB 检查退出 |
| `--reload` 热载 | 旧进程死亡 = 孤儿 `running`；新进程启动 sweep 标记 `executor_lost`。与既有安全网行为一致 |
| 同用户双端登录 | 第二条连接 claim 失败转旁听，收到同一事件流与同一终态；计量事件只由执行器产生（恰好一次） |

## 3. 对外契约

零变更：端点、请求/响应结构、SSE 事件名、`Last-Event-ID`、心跳语义均不变；
`docs/product/AI问答对外接口-整理.md` 无需修订。行为级差异（均为新增可达状态）：

| 场景 | 行为 |
|---|---|
| 长时间无终态的旁听连接 | 约 600s 后收到 wire-only `interaction.failed`（`follow_timeout`） |
| 执行者死亡的孤儿交互 | 启动 sweep / 旁听自愈标记 `failed`（`executor_lost`） |
| 断线后的结果 | 仍会落库，重连经 `Last-Event-ID` 或刷新恢复 |
| 超预算的交互 | `failed`（`run_timeout`），不再发起后续 MES 调用 |

## 4. 后果与风险

- 正面：连接抖动不再杀死执行；前端/代理读超时只影响旁听体验，可安全调短
  （验收时拍板，建议 120–300s，不建议 60s）。
- 风险一：执行器 commit 为无条件 upsert，若旁听自愈与执行器提交竞态，理论上存在
  「复活」窗口；`run_timeout < stale_running` 与停止点 DB 检查把窗口压缩到可忽略。
- 风险二：旁听自愈检查对发起者连接同样生效（发起者现在也是旁听者）；单次调用超过
  `stale_running_seconds` 的极端场景会被误标记 `executor_lost`，随后执行器停止点
  检查发现终态并静默退出，不会产生双重终态。
- 后台任务随进程死亡：与既有语义一致，sweep 兜底。
