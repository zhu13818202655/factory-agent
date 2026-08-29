# Factory Agent Kanban Board

## 待办

### 制作测试集

  - due: 2026-08-26
  - tags: [data]
  - priority: high
  - workload: Normal
  - defaultExpanded: false
  - steps:
      - [ ] 用AI总结期望的功能+目前提供的API
    ```md
    根据功能制作测试集，只做用户QA
    ```

## 进行中

## 已完成

### Story 9：usage-admin 用量看板与租户主数据

  - due: 2026-08-29
  - priority: high
  - workload: Normal
  - defaultExpanded: false
    ```md
    已完成：`tenant_registry` / `platform_principal` 建表（独立版本表
    `alembic_version_usage_admin`）；工厂账户管理六接口（写仅 admin、全部落审计、停用不删除、
    AppKey 出参脱敏前 6 位 + ***）；平台账号注册/登录与 Bearer token 双通道鉴权
    （`USAGE_ADMIN_API_TOKEN` 供前端）；用量查询 `/usage/mes-categories`、
    `/usage/mes-failures`、`/usage/by-tenant`、`/usage/mes-operations`、`/usage/models`、
    `/usage/capabilities`、`/usage/errors`；导出支持 MES 分类指标；前端对接文档
    `usage-admin/docs/API.md` 状态更新。单元/契约测试 786 项通过，Ruff/Pyright/Bandit 通过。

    已知限制：MES 分类统计读取的 `mes_call_fact` / `mes_operation_category` 由 Story 11
    建表写入，本 Story 以默认分类映射工作，真实数据端到端验证在 Story 11 后联调。
    待人工评审（State: Resolved）。
    ```

### 根据用户最新信息整理接口文档

  - due: 2026-08-25
  - priority: high
  - workload: Normal
  - defaultExpanded: false
    ```md
    客户接口文档已整理为 `docs/reference/弘兆MES接口整体说明-V2.md`。

    已完成：Story 5 重写为「客户契约对齐与 Mock MES 全量实现」；Story 6 首条纵切改为
    FR-002/003 个人工资；Story 7 合并为其余 L1 能力；Story 8/9 按 K1~K7 修订；
    Story 1~4 加修订标注；产品、API、ADR-0005 与目录约定同步更新。

    待人工评审：FR-004 取消、达成率/量产状态/在册人数/次品列降级为 unavailable。
    ```

## 待客户确认

> 2026-08-25 更新：客户已答复问题集，结论整理为 `docs/reference/弘兆MES接口整体说明-V2.md`
> 的 M1~M20（已确认）与 K1~K7（范围决策）。以下只保留第五章仍未确认的 14 项。

### 🔴 测试 app_key（含加密形态）、测试账号与联调计划（B.2）；正式环境根地址（B.4）


### 角色枚举与「员工/管理/老板」三级展示映射（A.1）；凭证刷新后旧 token 是否立即失效（B.3）


### 合格/次品口径（仅手工账有 cp，C.5）；产量 fhsl/sssl 写入环节（C.10）；阶梯价或加成（C.11）


### 订单检索口径与模糊/简拼支持（C.6）；一人多订单的产量与工资区分（C.13）


### 在职/离职判断字段（C.7）；量产状态数据源（C.8）；目标产量与达成率数据源（C.9）


### Flag 默认取扫描日期还是审核日期（C.12）；GongziMxQuery 是否仍支持货号/工序等过滤参数（C.14）


### 已由客户答复关闭（不再阻塞）

- 身份 token 与接入方式（M1/M10/M15）、权限责任归 MES（M3/M11/M12/M19）、租户模型（M4）。
- 组织只有一层车间、无小组（M5）；不需要调岗与历史组织关系（K2）。
- 工资口径 je = sl × price、无底薪津贴扣款（M9/M18）；进度按扫码本地计算（M6/M18）。
- 员工端收入排名取消（M7）；允许自主组合多个只读 API（M16）。
- 报表导出走我方自有机制（K5）；推送与定时任务当前版本不做（M17/K1/K6）。
- 交期预警等级不做（K4）；性能上限当前版本不管（K3）；MoveMenuQuery 不使用（K7）。


## 执行规则

