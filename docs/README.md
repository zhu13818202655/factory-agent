# 文档导航

当前 `docs/` 只保留权威文档，客户原始交付稿与历史版本不再在仓库内留副本（git 历史可追溯）：

| 目录 | 负责回答 | 当前入口 |
|---|---|---|
| `product/` | 做什么、谁能看什么、口径确认、方案设计 | [`需求及方案整理.md`](product/需求及方案整理.md)（产品需求 + 客户确认结论）、[`AI问答对外接口-整理.md`](product/AI问答对外接口-整理.md)（客户 MES 接口契约，标准区） |
| `adr/` | 为什么采用某项难以逆转的技术或边界决策 | [`0001-repository-and-service-boundaries.md`](adr/0001-repository-and-service-boundaries.md)、[`0002-runtime-storage-and-migration-baseline.md`](adr/0002-runtime-storage-and-migration-baseline.md)、[`0003-usage-metering-and-admin-service.md`](adr/0003-usage-metering-and-admin-service.md)、[`0004-logging-configuration-and-tracing.md`](adr/0004-logging-configuration-and-tracing.md)、[`0005-local-aggregation-over-server-shortcut.md`](adr/0005-local-aggregation-over-server-shortcut.md)、[`0006-model-provider-access-without-proxy.md`](adr/0006-model-provider-access-without-proxy.md) |

实现按 [`.github/story/`](../.github/story/) 中 `#1` 到 `#3` 的顺序执行。Story 是实施清单，
`docs/product/` 两份文档是需求与接口契约依据；实施过程中新确认的口径只回写这两份文档，
不新开分散小文件。

## 权威顺序

发生冲突时依次采用：当前 Story、`SECURITY.md` 与已接受 ADR、`docs/product/`（客户已确认口径
与接口契约）、`ARCHITECTURE.md`、仓库规则文件、现有实现。仍未确认的口径只允许产生显式
`unavailable` 状态，不得自行补齐或使用 Mock 数字冒充。

## 新增文档原则

- 新产品规则与口径确认优先补入 `product/` 两份文档，避免按功能点建立大量小文件。
- 客户接口形态不设 OpenAPI/契约目录镜像（避免双份漂移）：服务健康端点发布面由
  `tests/integration/test_health_openapi.py` 守卫，用量事件字段卫生由
  `tests/unit/application/test_produced_usage_events.py` 守卫，以代码与测试为准。
- 只有影响长期边界且难以撤销的决策才新增 ADR。
- 部署和值班手册在生产接入阶段再建立 `runbooks/`，当前不提前扩目录。
