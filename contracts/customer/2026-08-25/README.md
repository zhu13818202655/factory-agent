# 客户原始接口文档归档

## 来源

- 文件：`AI问答对外接口.md`
- 归档日期：2026-08-25（客户文档更新日期）
- 原始位置：`docs/reference/AI问答对外接口.md`（保持同步副本）

## 用途

本目录是客户真实 MES 接口形态的**追溯基准**。Story 5 将
`contracts/mes-canonical.openapi.yaml` 从虚构的 Canonical 资源型契约重写为客户
27 个接口的真实形态时，所有差异（响应壳、认证链、字段名、分页语义、footer 合计）
均以本归档为唯一事实来源。

## 注意

- 本目录内容**只读**，不得修改。后续客户文档更新时新增日期目录。
- 客户文档中的 AppSecret 示例值、测试 app_key 等敏感值不得进入生产代码、
  日志或测试快照；Mock MES 仅使用确定性占位值模拟形态。
- 已确认结论以 `docs/reference/弘兆MES接口整体说明-V2.md` 的 M1~M20/K1~K7 为准；
  第五章 14 项未确认项在实现中只允许产生显式 `unavailable`/`unconfirmed` 状态。
