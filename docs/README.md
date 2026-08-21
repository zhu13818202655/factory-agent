# 文档导航

当前 `docs/` 保留三类权威文档，不再继续拆分层级：

| 目录 | 负责回答 | 当前入口 |
|---|---|---|
| `product/` | 做什么、谁能看什么、哪些口径待确认 | [`requirements.md`](product/requirements.md)、[`permission-matrix.md`](product/permission-matrix.md)、[`traceability.md`](product/traceability.md) |
| `api/` | 客户 MES 必须提供什么、已经向客户确认什么 | [`customer-api-requirements.md`](api/customer-api-requirements.md)、[`customer-confirmation-questionnaire.md`](api/customer-confirmation-questionnaire.md) |
| `adr/` | 为什么采用某项难以逆转的技术或边界决策 | [`0001-repository-and-service-boundaries.md`](adr/0001-repository-and-service-boundaries.md) |
| `reference/` | 客户原始材料和历史整合文档，仅用于追溯来源 | [`工厂智能体需求与接口整合文档.md`](reference/工厂智能体需求与接口整合文档.md) |

实现按 [`.github/story/`](../.github/story/) 中 `#1` 到 `#9` 的顺序执行，日常状态见
[`kanban-board.md`](kanban-board.md)。Story 是实施清单，产品与 API 文档是需求和契约依据；
看板只展示状态，不复制验收内容。

## 权威顺序

发生冲突时依次采用：当前 Story、`SECURITY.md` 与已接受 ADR、`contracts/`、
`product/`、`ARCHITECTURE.md`、现有实现。`reference/` 不直接作为实现依据；其中未确认的客户
描述必须先进入产品规则、API 要求或 ADR，才能成为实施约束。

## 新增文档原则

- 新产品规则优先补入现有 `product/` 文档，避免按功能点建立大量小文件。
- 客户真实 OpenAPI 和样例归档到 `contracts/customer/<version>/`，不放入 `docs/api/`。
- 只有影响长期边界且难以撤销的决策才新增 ADR。
- 部署和值班手册在生产接入阶段再建立 `runbooks/`，当前不提前扩目录。