# factory-agent 路线图

> 更新日期：2026-08-25
> Story 目录：`.github/story/`

## 推进方式

- 一共 10 个 Story，按 `#1.md` 到 `#10.md` 的编号顺序推进。
- 每个 Story 直接说明要做什么、当前假设、具体步骤和大概能完成什么。
- 客户接口与已确认口径以 `docs/reference/弘兆MES接口整体说明-V2.md`（M/K 编号）为准；
  仍未确认的口径只允许产生显式 `unavailable` 状态，不得用 Mock 数字冒充。
- 完成一个步骤就勾选对应待办；一个 Story 做完后由用户人工查看并提出调整。

采用“先全局骨架、再纵向填充”的推进方式：Story 1 先建立所有模块的目录、Protocol、
配置入口、空实现和测试入口；Story 2~4 填充权限、执行、会话和事件生产基础设施；
Story 5 在客户接口文档到达后把契约、模型、身份与 Mock MES 整体对齐到客户真实形态；
Story 6 用第一条真实业务链路验证骨架；Story 7~8 再逐项增加业务能力、使用运营和增强功能；
Story 9 把 Mock MES 升级为 PG 持久化 + 每日定时生产化数据生成的数据底座；
Story 10（原 Story 9）接入客户真实环境并上线。权限、租户隔离和敏感数据规则不得留空或自行假设。

## Story 顺序

1. `#1.md`：全局工程骨架、契约与 Mock MES。
2. `#2.md`：可信身份、三级权限与数据安全。
3. `#3.md`：Text-to-API 数据边界与执行内核。
4. `#4.md`：会话编排、模型网关与 report-agent 复用。
5. `#5.md`：客户契约对齐与 Mock MES 全量实现。
6. `#6.md`：首条纵切（个人工资汇总与明细）、可信结果与 Excel。
7. `#7.md`：其余 L1 查询能力（员工、管理、老板）。
8. `#8.md`：使用运营、历史收藏、缓存与质量加固。
9. `#9.md`：Mock MES 生产化数据生成与 PostgreSQL 落地（插入项）。
10. `#10.md`：客户环境联调与生产上线（原 `#9.md`，因插入新 Story 9 顺延重编号）。

Story 1~4 完成于客户接口文档交付前，其中基于临时 Canonical 假设的结论已由 Story 5 修订，
各文件顶部已标注被修订条目。

## report-agent 迁移路线

`/home/admin2/proj/report-agent` 只作为迁移来源，不成为依赖或 git 子模块。迁移以行为、协议
和测试为单位，不整体复制包：

1. Story 1 盘点来源模块并建立工厂领域目标目录、Protocol、配置和测试替身。
2. Story 2 重写可信身份、`DataScope` 和审计；不迁移请求体中的 `tenant_id/user_id`，也不迁移
   `AllowAllPermissionGate` 默认行为。
3. Story 3 只借鉴只读校验和结构化结果思路；不迁移 Vanna、Text-to-SQL、TDengine/PostgreSQL
   业务取数代码或飞行业务 SQL。
4. Story 4 迁移纯状态转换、interaction/message 模型、会话仓储接口、上下文补丁、错误分类、
   SSE 事件和 OpenAI-compatible 测试方式，并按可信身份和持久化事件重写。
5. Story 6 迁移 renderer/router 的模式，重新实现 `ResultTable -> card/XLSX`，不迁移 DOCX、PDF、
   图表和飞行报告模板。

每批迁移都先为来源行为建立 characterization test，再迁入工厂领域命名，最后删除对
`report_agent` 的任何 import。来源仓库后续变化不会自动同步。

Story 文件里的待办勾选情况是唯一进度记录，不再维护单独的实施看板或机器评估结果。
