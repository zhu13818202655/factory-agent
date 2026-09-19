# ADR-0008：越权护栏并入 EXTRACT 调用（合并 + 独立兜底）

- 状态：Accepted（2026-09-14 实现；Task A / T5.1~T5.7）
- 日期：2026-09-14
- 所有者：项目维护者
- 关联：`docs/design/链路优化方案.md`（任务 A、阶段 5）、
  `src/factory_agent/application/scope_guard.py`、
  `src/factory_agent/application/intent.py`、
  `src/factory_agent/application/session/pipeline.py`、
  `src/factory_agent/application/session/consistency.py`、
  ADR-0004（日志）、ADR-0007（交互执行器）

## 1. 背景（现状，已验证）

一次业务查询的 LLM 往返次数是链路耗时的主项。改造前，鉴权通过之后、业务取数之前，
管线会**独立**发起一次 `SCOPE_GUARD` 调用做越权判定
（`application/session/consistency.py`），随后再发 `EXTRACT` 解析能力与槽位——
两者共用同一模型、同一上下文，只是一次多余的往返。

`SCOPE_GUARD` 的失败方向是 **fail-open**：`ModelGatewayError` / `StructuredOutputError`
只记 warning 后继续执行，且**不产生** usage 事件。

## 2. 决策

### 2.1 判定规则逐字搬迁，不重写口径

把 `SCOPE_GUARD_SYSTEM_PROMPT` 的判定规则与输出契约抽成两个共享常量
（`SCOPE_JUDGEMENT_RULES` / `SCOPE_OUTPUT_CONTRACT`），由独立调用与合并调用**同一份**
拼装。判定口径、值域、拒绝方向均不改写——本次变更只改变「在哪里问」，不改变「怎么判」。

### 2.2 合并为主路径，独立调用保留为二级兜底

- 合并模式（默认）：`EXTRACT` 的 system prompt 追加 scope 段落，要求 payload 多返回一个
  `scope` 子对象；解析结果放进 `ParsedIntent.scope_verdict`。
- 兜底触发条件：`scope` 键**缺失或非法**（`parse_scope_classification` 返回 `None`）→
  回落发起一次独立 `SCOPE_GUARD` 调用。
- **绝不把 `None` 当成「在范围内」**：缺失/非法一律视为「本次没拿到判定」，必须走兜底。

### 2.3 合并判定只能收紧，不能放宽

`_merged_scope_denial` 在本地完成拒绝文案，语义与独立调用一致：`beyond=true` 才拒绝，
`false` 或缺失都不放宽任何既有权限。拒绝文案仍由本地权限矩阵生成，模型不产出用户可见文案。

### 2.4 失败方向不变（fail-open）

兜底调用失败 → warning + 继续执行；合并调用本身失败（EXTRACT 整体非法）→ 走既有
REPAIR/`model_output_invalid` 路径，与改造前对 EXTRACT 的处理完全一致。

### 2.5 计量口径变更

- 合并成功路径**不发**独立 `SCOPE_GUARD` 事件；改为在 `EXTRACT` 的
  `llm_call_completed` payload 上带 `includes_scope: true` 与 `scope_verdict`（JSONB，**无需迁移**）。
- 兜底路径仍发 `SCOPE_GUARD` 事件——其语义变为「本次走了独立护栏」，而不是「护栏存在」。
- 计量投影**只带 verdict**，不带 target 文本：护栏的目标描述可能含越权线索，不进事件表。

### 2.6 开关

`FACTORY_AGENT_SCOPE_GUARD_MODE=merged|dedicated`，默认 `merged`。
`dedicated` = 强制回到独立调用（改造前行为），用于回退与灰度对照。

## 3. 不变式核对

| 不变式 | 结论 |
|---|---|
| 授权先于任何业务数据调用 | 成立：无论合并还是兜底，判定都在 `_runner.run` 之前 |
| 越权只收紧不放宽 | 成立：verdict 值域、判定规则、失败方向均不变；拒绝文案仍本地生成 |
| 敏感字段不进 prompt/日志/快照 | 成立：合并只多要一个 verdict，不多读业务数据；计量只存 verdict |
| 实时/重放/历史三路一致 | 成立：事件序列与 `InteractionCommit` 语义未动，只改变事件条数 |
| 审计口径可解释 | `llm_call_fact` 每交互业务查询由 3 行降为 2 行——**属对外报告口径变更**，须同步更新统计口径说明 |

## 4. 后果与风险

- 正面：正常路径每次业务查询少一次 LLM 往返（`llm_call_fact` 3 → 2）。
- 风险一（可用性耦合）：EXTRACT 的 JSON 一旦非法，会同时丢掉能力与护栏。已由独立兜底 +
  REPAIR 一次把影响压回可接受水平；灰度期须盯 `model_output_invalid` 频次是否上升。
- 风险二（拦截能力退化）：合并后模型可能「顺手」给出 `within`。灰度期须盯 `beyond`
  判定占比是否骤降；该指标是拦截能力是否退化的**首要信号**。
- 风险三（口径误读）：`llm_call_fact` 行数变化会被误读为「调用变少=质量下降」。已在本
  ADR 与计量字段（`includes_scope`）中显式记录，报告侧按新口径解读。
- 回退：置 `FACTORY_AGENT_SCOPE_GUARD_MODE=dedicated` 即回到改造前行为，无需回滚代码。
