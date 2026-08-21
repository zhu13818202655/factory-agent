# Factory Agent Kanban Board

> 更新日期：2026-08-21
>
> 本看板是人工执行视图，不是机器完成门禁。详细范围和 checklist 以对应 Story 为准；
> 只有实现、相关测试和文档完成后，才勾选 Story 中的项目并移动卡片。

## 进行中

| Story | 当前目标 | 下一步 |
|---|---|---|
| [#1 Canonical 契约与可复现 Mock MES](../.github/story/%231.md) | 工程骨架已完成；建立 A1~A3、C1~C8 Canonical 契约与确定性 Mock | 先定义公共分页/错误 Schema，再实现身份、组织和业务资源 OpenAPI |

## 待办

| 顺序 | Story | 开始条件 | 主要交付 |
|---:|---|---|---|
| 2 | [#2 可信身份、三级权限与数据安全](../.github/story/%232.md) | Story 1 身份/组织契约和 fixture 可用 | `Identity`、`DataScope`、权限矩阵、零业务调用拒绝、脱敏与审计基线 |
| 3 | [#3 Text-to-API 数据边界与执行内核](../.github/story/%233.md) | Story 1 资源 API 可调用；Story 2 范围模型稳定 | `MesDataSource`、Canonical Adapter、Catalog、L1 DAG、分页、DuckDB、`ResultTable` |
| 4 | [#4 会话编排、模型网关与 report-agent 复用](../.github/story/%234.md) | Story 2 权限入口、Story 3 capability 接口稳定 | 迁移会话/SSE/状态机/上下文，改造 intent/slot，建立 Fake LLM 与 LiteLLM 边界 |
| 5 | [#5 首条纵切、可信结果与 Excel](../.github/story/%235.md) | Story 1~4 公共链路可用 | FR-007 从文本到多 API、聚合、卡片、XLSX、审计的完整纵切 |
| 6 | [#6 员工查询能力](../.github/story/%236.md) | Story 5 纵切和导出模式通过 | FR-001~004 个人产量、工资汇总/明细、本人组内排名 |
| 7 | [#7 管理与老板查询能力](../.github/story/%237.md) | Story 6 的工资/产量口径组件可复用 | FR-005~012 其余能力，完成首期 12 项 L1 |
| 8 | [#8 历史收藏、缓存与质量加固](../.github/story/%238.md) | 12 项 L1 稳定并有 golden | 快捷问题、历史收藏、授权缓存、评测、故障与性能基线 |
| 9 | [#9 客户 API 接入与生产上线](../.github/story/%239.md) | 客户交付版本化契约、脱敏样例和测试环境 | `CustomerMesAdapterV1`、双跑核对、生产 SLO/runbook 和人工准入 |

## 已完成

当前没有完整完成的 Story。Story 1 已完成工程骨架、健康检查和本地启动方式，剩余项目继续在
“进行中”列执行。

## 待客户确认

这些事项不阻止按 Canonical 契约和 Mock MES 开发，但会阻止相应口径转为正式客户行为：

> 已发送客户的问题以 [CQ-01~CQ-24 问题集](api/customer-confirmation-questionnaire.md)为主跟踪表；
> 截至 2026-08-21 全部待答复，所需资料 MAT-01~MAT-08 全部待提供。

- 身份 token、角色编码、管理闭包、一人多组与调组规则。
- 次品/返工字段、阶梯价或加成、结算与“应发工资”口径、舍入和日均分母。
- 订单号与计划单号关系、进度值覆盖范围、计划量/达成率和量产状态。
- 交期预警阈值、最大日期跨度、性能 SLA 和 Redis 失效要求。
- 报表存储/下载接口归属、历史收藏持久化归属。
- 每日早报与工资推送是否进入本期，以及渠道和触发时点。
- 客户 API 的稳定 ID、批量过滤、授权范围过滤、完整分页、限流和变更通知。

需求来源中的历史疑问见[整合文档](reference/工厂智能体需求与接口整合文档.md) Q01~Q20；
技术底线见[客户 API 前置条件](api/customer-api-requirements.md)。

## 执行规则

1. 始终执行编号最小且仍有未完成项的 Story；未经明确决定不跨 Story 开工。
2. 每次只在实现和相关测试完成后勾选对应 checklist，不按文档数量或代码行数判断完成。
3. 客户规则缺失时继续使用 Canonical + Mock，并在结果和指标版本中标明临时假设。
4. 权限、租户、敏感字段、保留期、出站地址、凭据和生产切换变化必须人工批准。
5. 完成一个 Story 后运行其相关工程检查，更新本看板，并交给用户人工复核。
