# 客户 MES 字段字典与产量分语境口径表

来源：`docs/reference/弘兆MES接口整体说明-V2.md` 第 1.5 节（M1~M20 已确认）。
YAML 双份维护于 `configs/knowledge/field-dictionary.yaml`，供加载器校验；
本 Markdown 供人工评审。两份内容必须一致。

## 通用字段字典

| 字段 | 含义 | 字段 | 含义 |
|---|---|---|---|
| `uid` / `uname` | 员工工号 / 姓名 | `dept` / `deptname` | 部门（车间）编号 / 名称 |
| `huohao` / `bbreed` | 货号编号 / 货号名称 | `description` / `spname` | 品名 |
| `name_pk` | 简拼（货号/工序/员工/部门均有） | `sctype` / `sctypename` | 生产类型 |
| `worktype` / `wtname` | 工序编号 / 工序名称 | `color` / `chima` | 颜色 / 尺码 |
| `chuanghao` / `baohao` / `ganghao` | 床号 / 包号 / 缸号 | `dw` | 单位 |
| `rq` / `inputtime` / `check_time` | 业务日期 / 刷卡时间 / 审核时间 | `ischeck` | 是否审核（工资明细） |
| `price` / `je` | 工价 / 金额（**je = sl × price**，M9） | `cp` | 次品数（仅手工账，C.5 未确认） |
| `dh` / `jhdh` | 单号 / 计划单号（计划） | `dddh` / `ddh` | 计划单号（制单/吊挂/手工账） |
| `khddh` / `khhh` | 客户订单号 / 客户货号 | `khid` / `khname` | 客户编号 / 客户 |
| `ddsl` / `zsl` / `paol` | 订单数量 / 总数量 / 抛量（计划） | `finish_date` / `zhdate` | 交货日期 / 下单(制单)日期 |
| `state` | 单据状态（1=审核） | `cid` | 条码 ID（扫码/进度） |
| `sffb` / `fbid` | 是否分包 / 分包 ID（已扫描） | `zpsl` | 正品数量（进度） |
| `drdg_status` | 导入吊挂（0否/1是，制单） | `sfjz` | 是否结账（吊挂） |
| `gxtype` | 流转类型（0工序/1仓库/2验布工资专用） | `section` | 工段 |
| `default_price` / `xz_price` | 默认工价 / 限制工价（工序） | `gongzi_js_type` | 工资结算方式（0预发/1实收/2多次实收） |
| `default_working_hours` / `theoretical_work_hours` | 理论工时（工序/货号工序） | `vehicle_type` | 车种 |
| `sfzb` / `zhgx` / `sfxs` / `using_state` | 是否整版/最后工序/是否线上/使用中 | `move_admin_role` | 移动管理员（01是/02否；「00」限查本人，M19） |
| `employeeRule` | 分配角色 JSON 数组字符串（展示用途，A.1 待确认） | `move_Login` / `movepassword` | 移动登录权限 / 移动登录密码（敏感） |
| `loginUserName` | 登录用户绑定（员工） | `lpinpai` / `jst_huohao` | 品牌 / 聚水潭货号（货号） |
| `pid` / `sysdept` / `company` | 部门上级 / 系统部门（本厂）/ 公司 | `js` | 件数（手工账） |

## 产量数量字段分语境表

同一字段在不同接口语境下含义不同。能力实现必须按下表取数，
不得跨语境混用：

| 语境（接口） | `fhsl` | `sssl` | `sl` |
|---|---|---|---|
| 生产制单 Sclzd/GridPageList | 预发数量 | **产量** | — |
| 线下扫码明细 BarcodeClQuery | 预发数量 | 产量（扫码） | 预发数量 |
| 已扫描 YskQuery | 发卡数量 | — | **产量**（工资口径，je=sl×price） |
| 未扫描 WskQuery | — | — | 预发数量（待扫数量） |
| 工序进度 WorktypeProgressQuery | 预发数量 | — | — |
| 手工账 PinFeng/GridPageList | — | — | 数量（件数为 `js`） |
| 生产计划 Plan/GridPageList | — | — | 预发数量 |
| 吊挂 DgClQuery | — | — | 预发数量 |

我方暂定口径：个人产量/工资以 `sl` 为准；制单/订单「完成数量」以
`sssl` 为准；细口径联调确认（第五章 C.10）。

## 已确认指标口径（status: confirmed）

| 指标 | 公式 | 来源 |
|---|---|---|
| `payroll.amount` | `sl × price` | M9/M18 |
| `payroll.gross_total` | `footer.je_total`（GongziMxQuery/YskQuery/GongziJeOrderQuery） | M9/M13 |
| `output.personal` | 明细行 `sl` 合计（YskQuery/BarcodeClQuery 语境） | M18/C.10 暂定 |
| `output.order_completed` | `sssl` 合计（Sclzd 语境） | M18/C.10 暂定 |
| `progress.ratio` | 已扫码工序数（`uid` 非空）/ 总工序数（HuohaoWorktypeQuery） | M6/M18 |

## 未确认指标口径（status: unconfirmed）

被能力引用时必须产生显式 `unavailable` 列状态，禁止渲染为数字：

| 指标 | 缺口 | 来源 |
|---|---|---|
| `quality.defective` | 仅手工账有 `cp` 字段，无统一合格/次品数据源 | 第五章 C.5 |
| `plan.target_output` | 目标产量无数据源（zsl/ddsl/sl 以哪个为准未答复） | 第五章 C.9 |
| `org.headcount` | 在册人数在职/离职判断字段未答复 | 第五章 C.7 |
| `production.stage` | 量产状态（试产/量产）无数据源 | 第五章 C.8 |
| `time.flag_default` | GongziMxQuery `Flag` 默认 0 扫描日期 / 1 审核日期未定；本期默认 0 并在卡片标注口径 | 第五章 C.12 |
