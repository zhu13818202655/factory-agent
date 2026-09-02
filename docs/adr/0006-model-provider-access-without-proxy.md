# ADR-0006：无 LiteLLM Proxy 时的模型供应商接入边界

- 状态：Accepted
- 日期：2026-08-25
- 所有者：项目维护者
- 关联：ADR-0004（LLM 供应商配置边界以本文档为准）

## 1. 背景

本项目不部署 LiteLLM Proxy（`deploy/compose/compose.yaml` 与 `deploy/k8s/` 中都没有该服务），
但应用需要一个承担可靠性（fallback / 重试 / 冷却）的模型路由层：直连单个供应商无法在多
供应商配置或供应商故障时切换。

同时确认：本项目只接入 OpenAI-compatible 协议的供应商（DeepSeek 等自身即为该协议），
不需要 Bedrock、Vertex 等异构 SDK 形态。

## 2. 决策

引入 `litellm` SDK，并使用 `litellm.router.Router` 承担可靠性层。边界随之调整：

1. **应用持有供应商配置**。取消"应用不得拥有供应商 URL"的限制。模型部署清单由
   `configs/knowledge/models.yaml` 评审后定义，包含逻辑别名、上游模型名、`api_base`
   和优先级。
2. **密钥只以环境变量引用出现**。配置文件写 `api_key_env: FACTORY_AGENT_LLM_KEY_<NAME>`，
   永远不写密钥字面量；进程启动时解析为 `SecretStr`。Git 中不存在任何密钥。
3. **fallback、重试与冷却委托给 Router**。按逻辑别名声明有序 fallback 链；429 使用指数
   退避；失败部署进入冷却。应用自身仍然不写重试循环。
4. **逻辑别名不变**。`factory-fast`、`factory-reasoning`、`factory-summary` 仍是应用侧唯一
   可见的模型标识，业务代码不认识具体供应商。
5. **兼容既有 Proxy 形态**。Router 的一个部署项可以指向 LiteLLM Proxy；若未来引入
   Proxy，仅需把 `models.yaml` 收敛为单个指向 Proxy 的部署项，无需改动业务代码。

## 3. 不变的约束

以下 ADR-0004 与 `AGENTS.md` 的约束继续完全有效，并由测试强制：

- prompt、completion、请求体、供应商密钥永远不进入日志、trace、错误消息、usage 事件或
  测试快照；`litellm` 的全局 callback 与 verbose 日志必须显式关闭。
- 应用只记录脱敏事实：逻辑别名、实际模型名、尝试次数、token 数、耗时、fallback 原因、
  错误类别。
- LLM 不得构造原始 URL、鉴权头或不受限 SQL。
- 模型网关仍然只通过 `ModelGateway` port 暴露；`application/` 不认识 `litellm`。
- schema 校验与至多一次语义修复仍属于应用，不属于 Router。

## 4. 后果

- 正面：系统具备真正 fallback；退避、冷却、健康跟踪等易错逻辑由成熟实现承担；
  逻辑别名与业务代码不受供应商变更影响。
- 代价：`litellm` 引入较多传递依赖（其中 `boto3`/`botocore`/`s3transfer` 与
  `huggingface-hub`/`tokenizers` 对本项目无用）。
- 代价：`litellm` 未从顶层导出 `Router`，且 `acompletion` 的类型为 `**kwargs: Unknown`，
  Pyright strict 下需要从 `litellm.router` 导入并使用定点 ignore。该成本被限制在
  `llm/router_gateway.py` 单个模块内。
- 风险：应用进程内持有供应商密钥，泄漏面相对集中。由 `SecretStr`、脱敏日志
  与 canary 测试控制。
- 推翻条件：项目决定部署 LiteLLM Proxy 且不再需要应用侧路由。届时把 `models.yaml`
  收敛为单个指向 Proxy 的部署项即可，无需改动业务代码。

## 5. 关联影响

- ADR-0004 的 "LLM Config Boundary" 内容已按本文档更新，供应商配置边界以本文档为准；
  其日志与追踪决策不受影响。
- `FACTORY_AGENT_LITELLM_BASE_URL` 与 `FACTORY_AGENT_LITELLM_API_KEY` 由
  `configs/knowledge/models.yaml` 与 `FACTORY_AGENT_LLM_KEY_*` 取代。
