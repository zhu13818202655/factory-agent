# 客户 MES API 接入事实

> 更新日期：2026-08-25
> 依据：`docs/reference/弘兆MES接口整体说明-V2.md`（V2 说明，M1~M20 / K1~K7 / 第五章）与
> `docs/reference/AI问答对外接口.md`（客户原始接口文档，2026-08-25 更新版）。
>
> 本文记录**客户已交付接口的既成事实**及其对我方设计的约束。它不再是「向客户提出的前置条件
> 清单」——那份清单已由客户答复取代，答复记录见
> [`customer-confirmation-questionnaire.md`](customer-confirmation-questionnaire.md)。

## 对接总览

| 项目 | 事实 | 来源 |
|---|---|---|
| 测试环境根地址 | `http://hzlinkbiz.ywhzsoft.com:9002` | 客户确认 |
| 正式环境根地址 | 客户联调前提供 | 第五章 B.4，待提供 |
| 请求方式 | 全部 `POST` + `Content-Type: application/json` | V2 §1.1 |
| 认证 | Header `Authorization: Bearer {accessToken}`；Body 带 `app_key`/`timestamp`/`sign` | M1 |
| 凭证入口 | `/api/system/token`，入参为用户访问智能体时携带的加密 app_key | M15 |
| 凭证内容 | accessToken(2h) / sign / timestamp / 明文 appkey / `user`(工号) / `uname` / 空 `roles` / 空 `permissions` | M1/M2/M15 |
| 租户模型 | AppKey 即租户 ID，一厂一 Key | M4 |
| 签名算法 | 客户侧内部实现，我方不感知 | M8 |
| 接口总数 | 27 个（`MoveMenuQuery` 当前版本不使用） | V2 §2 / K7 |

## 响应与错误语义

```json
{ "code": 1, "message": "成功", "result": { "list": [], "total": 0 }, "timestamp": 1786773213 }
```

- 成功判断两层（M14）：先看 HTTP status code（`200` 正常、`404` 接口地址错误），再看响应体
  `code`（`1` 成功取 `result`，`0` 失败按 `message` 区分）。
- 列表接口均返回 `result.total`（M13），部分带 `result.footer` 合计。
- `code=0` 的可区分 `message`：`app_key不能为空` / `请求已过期` / `签名无效` / `无效app_key` /
  `加密信息解析失败`。
- **权限不足没有专门错误码**，表现为按配置过滤后无数据（M12）。

## 接口清单（27 个）

| 分类 | 接口 |
|---|---|
| 认证与凭证（3） | `/api/system/token`、`/api/print/query-sign`、`/api/print/test-permissions` |
| 基础数据（9） | `UserInfoQuery`、`MoveMenuQuery`（不使用，K7）、`HuohaoQuery`、`HuohaoFormQuery`、`ScTypeQuery`、`RfidWorktypeQuery`、`HuohaoWorktypeQuery`、`EmployeeQuery`、`DeptQuery` |
| 生产计划与制单（4） | `Plan/GridPageList`、`Sclzd/GridPageList`、`SclzdWorktypeQuery`、`SclzdBarcodeQuery` |
| 产量与进度（6） | `BarcodeClQuery`、`HuohaoWtCLQuery`、`PinFeng/GridPageList`、`WorktypeProgressQuery`、`YskQuery`、`WskQuery` |
| 工资与排名（2） | `GongziMxQuery`、`GongziJeOrderQuery` |
| 吊挂（3） | `Dg/GridPageList`、`DgZuGridPageList`、`DgClQuery` |

完整入参、返回要点与用途见 V2 说明第二章；字段含义与产量分语境口径见 `field-dictionary.md`。

## 可组合性达成情况

原前置条件清单在客户接口交付后的达成情况：

| 硬要求 | 达成 | 说明 |
|---|---|---|
| 稳定关联 ID | ✅ | `uid` 员工、`dept` 部门、`huohao` 货号、`worktype` 工序、`dh`/`jhdh`/`dddh` 单号、制单明细 `id`（物料编号）、`cid` 条码 ID |
| 批量 ID 筛选 | ⚠️ 部分 | 多数接口按时间区间 + 单一主键筛选；`WorktypeProgressQuery` 必须逐个物料编号查询，批量进度存在 N+1，需靠预算与分批控制 |
| 按授权范围过滤 | ✅（由 MES 承担） | 我方不下推范围条件，MES 按调用者身份过滤（M3/M19） |
| 可证明拉全的分页 | ✅ | `page`/`size` + `result.total`，累计达到 `total` 结束（M13） |
| 时间区间过滤 | ✅ | `dates`/`datee`；`Flag` 区分扫描日期与审核日期，默认口径待确认（第五章 C.12） |
| 只读、幂等、可超时 | ✅ | 当前提供的全部为只读查询接口（M16） |
| 稳定错误码与 `Retry-After` | ⚠️ | 只有 `code` 0/1 + `message` 文本，无细分错误码，无 `Retry-After`；由 Adapter 映射到统一异常 |
| 字段字典与枚举 | ⚠️ 部分 | V2 §1.5 已给出通用字段字典；角色枚举待确认（第五章 A.1） |
| 分页上限与限流策略 | ❌ | 客户未声明，我方 `size` 与最大页数为占位配置，Story 10 联调复核 |
| 脱敏响应样例 | ✅ | 客户原始文档含示例 |

## 已确认业务对象关系（M18）

```
生产计划(Plan) ──jhdh ↔ dddh── 生产制单(Sclzd) ── 制单明细(id = 物料编号)
                                                    │ 挂：货号 / 工序 / 床号
                    ┌───────────────────────────────┼───────────────────────────┐
              线下扫码产量                      吊挂产量(Dg)                 手工账(PinFeng)
             BarcodeClQuery                    DgClQuery                  GridPageList
                    └───────────────────────────────┼───────────────────────────┘
                                    工资三源合一 GongziMxQuery（Type=0/1/2）
                                    je = sl × price（M9/M18）
```

- 产量 = 实际扫描/录入数量；个人产量与工资口径取 `sl`，制单/订单完成数量取 `sssl`。
- 工序进度 = 主单 + 明细 + 工序 + 已扫条码合并；`uid` 非空 = 该工序已完成（M6）。
- 组织为单层车间（M5），`DeptQuery` 的 `pid` 仅用于展示归属。

## 仍未确认的接口与口径

见 V2 说明第五章共 14 项，其中影响实现的重点：

| 编号 | 问题 | 影响 |
|---|---|---|
| B.2 🔴 | 测试 app_key 与测试账号未交付 | 阻断真实联调，Story 10 无法开始 |
| B.3 | 凭证刷新后旧 token 是否立即失效 | 刷新策略 |
| B.4 | 正式环境根地址 | 生产配置 |
| A.1 | 角色枚举与三级展示映射 | 角色展示；不影响授权判定 |
| C.5 | 合格/次品口径（仅手工账有 `cp`） | FR-001 输出列 |
| C.6 | 订单检索口径与模糊/简拼支持 | FR-005/006 实体解析 |
| C.7 | 在职/离职判断字段 | FR-011 在册人数与人均 |
| C.8 | 量产状态数据源 | FR-010 输出列 |
| C.9 | 目标产量与达成率数据源 | FR-007/010 达成率 |
| C.12 | `Flag` 默认取扫描日期还是审核日期 | 全部时间口径 |
| C.13 | 一人多订单的产量/工资区分 | 事实粒度 |
| C.14 | `GongziMxQuery` 是否仍支持货号/工序等过滤参数 | FR-006/008 下钻 |

未确认口径在实现中必须输出结构化 `unavailable`/`unconfirmed`，不得用 Mock 数字冒充。

## 归档约定

客户原始文档与后续版本按日期归档到 `contracts/customer/<date>/`；契约以
`contracts/mes-canonical.openapi.yaml`（客户真实形态）为消费边界。客户字段名与错误
`message` 只允许存在于 `src/factory_agent/data_api/`。
