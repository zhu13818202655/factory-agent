# 弘兆 MES 对外接口整体说明（V2）

> 版本：V2
> 更新日期：2026-08-25
> 依据：客户《AI问答对外接口》2026-08-25 09:06 更新版（含文档末尾《智能体平台与 MES 系统 API 对接确认问题集》的客户答复，2026-08-24 14:50）及 2026-08-25 客户沟通确认
> 说明：第一~四章为**已确认**的部分（会议结论 + 客户书面答复 + 沟通确认，统一以 M 编号标注）；**第五章仅保留仍不清楚、需客户进一步解释的问题**；**第六章记录当前版本已确认的「已知条件与范围决策」**（K1~K7，作为后续版本的前置知识）。

## 一、对接总览

### 1.1 基本信息

| 项目 | 内容 |
| :--- | :--- |
| 服务根地址 | 测试环境：`http://hzlinkbiz.ywhzsoft.com:9002`（客户已确认）；正式环境地址由客户在联调前提供（见第五章 B.4） |
| 请求方式 | 全部为 `POST`，`Content-Type: application/json` |
| 认证方式 | Header 带 `Authorization: Bearer {accessToken}`；Body 带 `app_key` / `timestamp` / `sign` 公共参数（M1） |
| sign / timestamp | 取自 token 接口返回值，随每次业务请求携带；有效期由客户侧延长，具体数值不影响方案（我方有主动刷新机制保证，M1/M8） |
| 凭证获取 | 调用 `/api/system/token`（传入的 `app_key` 即用户访问智能体时携带的那个，客户确认），返回 accessToken（**有效期 2 小时**）、sign、timestamp、**用户对应传递的 appkey（明文）**（M15） |
| 租户模型 | **AppKey 即租户 ID**，一个工厂一个 AppKey；我方所有数据按 AppKey 归档存储（M4） |

### 1.2 统一请求结构

每个业务接口的 Body 都包含公共参数 + 业务参数（`app_key` 用 token 返回的**用户对应传递的 appkey（明文）**；`timestamp`/`sign` 用 token 接口返回的值；过期后重新调 token 接口刷新）：

```json
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  ...业务参数
}
```


### 1.3 统一响应结构

```json
{
  "code": 1,          // 1=成功，0=失败
  "message": "成功",  // 失败时用于区分具体原因（见 M14）
  "result": { ... },  // 业务数据；列表类为 { "list": [...], "total": 总数 }，部分带 "footer" 合计
  "timestamp": 1786773213
}
```

> **如何判断响应成功（两层，依次判断）**：
> 1. **HTTP status code**：`200` = 请求正常到达服务；`404` = 失败（接口地址/路径错误，核对根地址与接口路径）；
> 2. **200 响应体内 `code`**：`1` = 业务成功，取 `result`；`0` = 业务失败，按 `message` 区分具体原因（M14）。
>
> 列表接口均返回 `result.total`（M13），分页取数按「累计条数达到 total 结束」。

### 1.4 认证流程（已确认的机制）

```
用户在小程序访问智能体（跳转时携带「用户加密 app_key」）
    │
    ▼
智能体前端调用 /api/system/token（传入用户加密 app_key）
    │
    ▼
接口返回：accessToken（2 小时）+ sign + timestamp + 用户对应传递的 appkey（明文）
          + user(工号)/uname(姓名) + roles/permissions（空数组）
    │
    ▼
每次业务请求：
  Header:  Authorization: Bearer {accessToken}
  Body:    明文 appkey + sign/timestamp（取自 token 接口）+ 业务参数
    │
    └── MES 按 Token 身份自动做行级数据过滤

刷新机制：accessToken（2 小时）到期前主动刷新（如 90 分钟或遇过期类错误）；
          sign/timestamp 有效期由客户侧延长，过期后重新调 token 接口刷新。
```

> 凭证入口为 `/api/system/token`：小程序跳转携带**用户加密 app_key**，我方前端调它统一取得凭证；AppSecret 与签名算法由客户侧管理，我方无需感知；sign/timestamp 有效期由客户延长，我方以主动刷新机制保证可用。刷新细节（旧 token 是否立即失效等）见第五章 B.3。

**已确认的关键机制结论：**

| # | 结论 |
| :--- | :--- |
| M1 | **统一凭证**：前端调用 `/api/system/token`（入参 `app_key` = 用户访问智能体时携带的那个，客户确认）取得 `accessToken`（2 小时）/ `sign` / `timestamp` / **用户对应传递的 `appkey`（明文）**；业务请求 Header 带 `Authorization: Bearer {accessToken}`、Body 带 `app_key/timestamp/sign`；凭证过期（message：请求已过期）时重新调 token 接口刷新 |
| M2 | **accessToken（JWT）有效期 2 小时**（expiresIn=7200），到期前主动刷新；JWT 载荷（按客户文档示例 accessToken 解码）含 `user`(工号) / `uname` / `userType`(示例值「小程序用户」) / `customId`(即 appkey) / iat·nbf·exp（exp−iat=7200s，与 expiresIn 一致） |
| M3 | **行级数据权限由 MES 完成**：接口按调用者身份（company/dept/move_admin_role）自动过滤，我方无需拼接过滤条件（客户问题集佐证） |
| M4 | AppKey 即租户 ID，一厂一 Key；我方所有数据按 AppKey 归档存储 |
| M5 | 组织只有一层部门（车间 `dept`），**没有「小组」层级**；排名/对比粒度统一为车间 |
| M6 | 进度按扫码判定：工序记录 `uid` 非空 = 已扫码 = 该工序已完成；进度%由我方按「已完成工序数 / 总工序数」计算 |
| M7 | 员工端不做收入排名（接口只返回本人数据）；管理端排名直接用接口返回的倒序数据按顺序编号位次（排名接口返回 `dept`，可按车间分组） |
| M8 | **签名与密钥我方不管**：AppSecret 与签名算法为客户侧内部实现；`sign`/`timestamp` 统一取自 `/api/system/token` 返回值并随业务请求携带；sign/timestamp 有效期由客户延长，我方以主动刷新机制保证 |
| M9 | **工资口径**：只有一个计件工资，不存在底薪/津贴/扣款，「应发合计」= `je` 合计；客户确认**工资 = 数量 × 单价（sl × price）** |
| M10 | **登录与接入方式**：员工登录发生在**客户自己的小程序**内，**悬浮窗形式**集成；**跳转时小程序携带（用户加密）app_key 给我方**，我方前端凭它调 `/api/system/token` 获取凭证；**token 响应直接返回 `user`(工号) / `uname`(姓名)**，「本人」查询可直接使用，无需用户报工号 |
| M11 | **角色/权限完全由客户侧处理**：我方不关心用户角色（token 响应中虽有 `roles`/`permissions` 字段，当前返回空数组，我方仍不依赖） |
| M12 | **越权判定方式**：MES 接口返回数据已按权限过滤，我方按「返回范围明显小于请求范围 = 无对应权限」判断并给出友好提示；客户问题集佐证：**权限不足通常表现为按配置过滤后无数据**（无专门错误码） |
| M13 | **分页**：客户最新文档中列表接口**均返回 `total`**，按「有 total」设计，累计取到 total 即结束 |
| M14 | **成功判断两层**：先看 HTTP status code（`200`=正常，`404`=失败，查接口地址），再看 200 体内的 code（只有 0/1，1=成功，0=失败）；code=0 时 **message 可区分具体原因**：「app_key不能为空 / 请求已过期（凭证过期，重取刷新）/ 签名无效 / 无效app_key / 加密信息解析失败(用户携带数据有误)」；参数错误、无权限等细分场景由我方智能体平台按 message 封装处理 |
| M15 | **凭证获取接口（客户确认）**：`/api/system/token`，传入的 `app_key` 即用户访问智能体时携带的那个；请求后返回 accessToken(2h)/sign/timestamp/**用户对应传递的 appkey（明文）**/user/uname/roles(空)/permissions(空)；按客户说法由**前端实现**；`/api/print/query-sign`（传 app_key+timestamp）可作 sign 查询的备用手段（主路径为 token 接口返回的 sign） |
| M16 | **允许自主组合**：客户明确允许智能体平台**自主组合、关联、分析多个 MES 只读 API**（当前提供的全部为只读查询接口）；车间对比、趋势、达成率等聚合指标由我方拉明细自行计算（性能上限当前版本不管，见 K3） |
| M17 | **MES 侧无推送能力**：无用户订阅机制、无主动事件推送、内部任务调度器未启用；推送类功能（工资发布/日报/延期提醒）由我方**定时轮询**实现，渠道为**对接客户已有的消息渠道**；**当前版本不实现**（推送依赖定时任务，见 K1/K6），具体渠道与格式待后续版本实现时确定 |
| M18 | **业务对象对应关系（客户确认）**：生产计划 → 生产制单 → 制单明细；明细上挂货号（huohao）/工序（worktype）/床号（chuanghao）；员工通过 `uid` 与产量、工资关联；**产量 = 实际扫描/录入数量（sl/fhsl）；工资 = 数量 × 单价（sl × price）；工序进度 = 主单 + 明细 + 工序 + 已扫描条码合并后的完成进度** |
| M19 | **权限机制（客户问题集）**：「区分公司权限」开关实现公司级数据隔离；`move_admin_role="00"` 的角色进一步限定**只能查看本人数据**；部门/车间维度通过 `dept` 字段过滤；数据范围由 company/dept/move_admin_role 共同决定 |
| M20 | **用户标识与用户映射（客户问题集）**：稳定标识分两层——账号层 PIUSER.ID/CODE（`UserInfoQuery`：code/username/realname/companyName）、业务员工层 sys_employee.uid（`EmployeeQuery`）；MES 不提供会话/订阅服务，**我方可以保存最小用户映射**，用于会话、订阅与定时推送 |

### 1.5 核心业务对象关系（对应关系已由客户确认，M18）

```
生产计划(Plan)                          生产制单(Sclzd)
  单号dh / 计划单号jhdh                   单号dh / 计划单号dddh
  客户khid/khname/khhh / 客户订单号khddh    货号huohao / 床号chuanghao / 包号baohao / 缸号ganghao
  订单数ddsl / 总数量zsl / 预发sl           预发(发卡)fhsl / 产量sssl
  交货日期finish_date / 状态state(1=审核)     id = 物料编号
        │                                     │
        └────────── 对应关系（M18）─────────────┘
                       ▼
              制单明细（id=物料编号）
              上挂：货号 / 工序 / 床号
                       │
     ┌─────────────────┼──────────────────┐
     ▼                 ▼                  ▼
 线下扫码产量      吊挂产量(Dg)         手工账(PinFeng)
 BarcodeClQuery   DgClQuery           GridPageList
     └─────────────────┼──────────────────┘
                       ▼
 工资明细/汇总 GongziMxQuery（三源合一 Type=0扫码/1吊挂/2手工账；je = sl × price，M9/M18）
 工资排名 GongziJeOrderQuery（已按 je 倒序，含 dept）
 工序进度 WorktypeProgressQuery（主单+明细+工序+已扫条码合并，M18；按物料 userid 查）

组织（DeptQuery）：
公司 company（companyName）
  └─ 部门/车间 dept（DeptQuery：id/name/pid 上级/sysdept「本厂」）
       └─ 员工（EmployeeQuery：uid/uname/dept/move_admin_role/employeeRule）
```

**通用字段字典（各业务接口高频出现）：**

| 字段 | 含义 | 字段 | 含义 |
| :--- | :--- | :--- | :--- |
| `uid` / `uname` | 员工工号 / 姓名 | `dept` / `deptname` | 部门（车间）编号 / 名称 |
| `huohao` / `bbreed` | 货号编号 / 货号名称 | `description` / `spname` | 品名 |
| `name_pk` | 简拼（货号/工序/员工/部门均有，可做简拼匹配） | `sctype` / `sctypename` | 生产类型（整件/大身等） |
| `worktype` / `wtname` | 工序编号 / 工序名称 | `color` / `chima` | 颜色 / 尺码 |
| `chuanghao` / `baohao` / `ganghao` | 床号 / 包号 / 缸号 | `dw` | 单位 |
| `rq` / `inputtime` / `check_time` | 业务日期 / 刷卡时间 / 审核时间 | `ischeck` | 是否审核（工资明细） |
| `price` / `je` | 工价（单价）/ 金额（je = sl × price） | `cp` | 次品数（仅手工账） |
| `dh` / `jhdh` | 单号 / 计划单号（计划） | `dddh` / `ddh` | 计划单号（制单/吊挂/手工账） |
| `khddh` / `khhh` | 客户订单号 / 客户货号 | `khid` / `khname` | 客户编号 / 客户 |
| `ddsl` / `zsl` / `paol` | 订单数量 / 总数量 / 抛量（计划） | `finish_date` / `zhdate` | 交货日期 / 下单(制单)日期 |
| `state` | 单据状态（1=审核） | `cid` | 条码 ID（扫码/进度） |
| `sffb` / `fbid` | 是否分包 / 分包 ID（已扫描） | `zpsl` | 正品数量（进度） |
| `drdg_status` | 导入吊挂（0否/1是，制单） | `sfjz` | 是否结账（吊挂） |
| `gxtype` | 流转类型（0工序流转/1仓库流转/2验布工资专用，工序） | `section` | 工段（前道等） |
| `default_price` / `xz_price` | 默认工价 / 限制工价（工序） | `gongzi_js_type` | 工资结算方式（0预发/1实收/2多次实收） |
| `default_working_hours` / `theoretical_work_hours` | 理论工时（工序/货号工序） | `vehicle_type` | 车种（平车等） |
| `sfzb` / `zhgx` / `sfxs` / `using_state` | 是否整版 / 最后工序 / 是否线上 / 使用中（货号工序） | `move_admin_role` | 移动管理员（01是/02否；问题集：「00」限查本人数据） |
| `employeeRule` | 分配角色（JSON 数组字符串） | `move_Login` / `movepassword` | 移动登录权限 / 移动登录密码（员工） |
| `loginUserName` | 登录用户绑定（员工） | `lpinpai` / `jst_huohao` | 品牌 / 聚水潭货号（货号） |
| `pid` / `sysdept` / `company` | 部门上级 / 系统部门（本厂）/ 公司（部门） | `js` | 件数（手工账） |

**产量数量字段分语境（按文档语境标注）：**

| 语境（接口） | `fhsl` | `sssl` | `sl` |
| :--- | :--- | :--- | :--- |
| 生产制单 Sclzd | 预发数量 | **产量** | — |
| 线下扫码明细 BarcodeCl | 预发数量 | 产量（扫码） | 预发数量 |
| 已扫描 YskQuery | 发卡数量 | — | **产量**（工资口径，je=sl×price） |
| 未扫描 WskQuery | — | — | 预发数量（待扫数量） |
| 工序进度 WorktypeProgress | 预发数量 | — | — |
| 手工账 PinFeng | — | — | 数量（件数为 `js`） |
| 生产计划 Plan | — | — | 预发数量 |
| 吊挂 DgCl | — | — | 预发数量 |

> 客户问题集另表述「产量 = 实际扫描/录入数量（sl/fhsl）」，与上表个别语境标注（制单语境 sssl=产量）略有出入；我方暂定：个人产量/工资以 `sl` 为准（与 je=sl×price 对应），制单/订单「完成数量」以 `sssl` 为准，细口径联调确认（见第五章 C.10）。

---

## 二、接口清单（按业务分类）

> 共 **27 个接口**（其中用户菜单查询 `MoveMenuQuery` 当前版本不使用，见 K7）。除注明外均为 POST + 公共参数（app_key/timestamp/sign）+ Bearer Token。

### 2.1 认证与凭证类（3 个）

| 接口 | 地址 | 关键入参 | 返回 | 用途 |
| :--- | :--- | :--- | :--- | :--- |
| 获取访问令牌（智能体凭证入口） | `/api/system/token` | **app_key**（用户访问智能体时携带的那个） | accessToken（2h）/ sign / timestamp / **用户对应传递的 appkey（明文）** / user（工号）/ uname / roles（空）/ permissions（空） | 智能体获取凭证的唯一入口，本身无需签名（M15） |
| 获取签名 | `/api/print/query-sign` | app_key、timestamp（无需 sign） | `result` = 32 位小写 MD5 签名字符串 | sign 查询的备用手段（主路径为 token 接口返回的 sign，M1） |
| 测试权限 | `/api/print/test-permissions` | app_key、timestamp、sign + Bearer | 「调用成功」 | 联调自测鉴权链路 |

### 2.2 基础数据类（9 个）

| 接口 | 地址 | 关键入参 | 返回要点 | 用途 |
| :--- | :--- | :--- | :--- | :--- |
| 用户信息查询 | `/api/NetYf/Baseinfo/UserInfoQuery` | USERNAME（登录用户，必填） | list：code/username/realname/companyName + total | 账号层（PIUSER）用户信息；返回含角色相关字段（是否为用户角色、枚举值正与客户确认，见第五章 A.1）（M20） |
| 用户菜单查询（**当前版本不使用**） | `/api/NetYf/Baseinfo/MoveMenuQuery` | — | list：uid/uname/dept/menus（菜单名/模块/是否调扫描/排序）+ total | 当前版本忽略（见 K7）；接口保留，后续版本如需菜单式入口可复用 |
| 货号信息单据 | `/api/NetYf/Baseinfo/HuohaoQuery` | — | huohaoList（bh 编号/bbreed 货号/name_pk 简拼/description 品名/dw 单位/lpinpai 品牌/jst_huohao 聚水潭货号/isdelete）+ huohaoTypeList 货号类型 | 货号主数据全集，款号解析与候选匹配（含简拼） |
| 货号信息表单 | `/api/NetYf/Baseinfo/HuohaoFormQuery` | huohao | 货号 + 颜色列表（含图片 guid）+ 尺码列表（版型 banx/克重 kez/销售价 xs_price/单价 price） | 单个货号详情与尺码价 |
| 生产类型 | `/api/NetYf/Baseinfo/ScTypeQuery` | — | sctypeList（bh/name/简拼/是否计裁剪 sfjcj/是否成品入库 sfcprk）+ total | 生产类型字典 |
| 生产工序 | `/api/NetYf/Baseinfo/RfidWorktypeQuery` | — | worktypeList（bh/name/工段 section/流转类型 gxtype/默认工价 default_price/限制工价 xz_price/工资结算方式 gongzi_js_type/理论工时/车种）+ total | 工序字典、默认工价与结算方式（工价口径，M18） |
| 货号工序 | `/api/NetYf/Baseinfo/HuohaoWorktypeQuery` | huohao | list：wt/wtname/sort 序号/生产类型/是否整版 sfzb/最后工序 zhgx/是否线上 sfxs/使用中/理论工时 + total | 款式工序路线，进度计算的总工序数依据 |
| 员工信息 | `/api/NetYf/Baseinfo/EmployeeQuery` | uid（可选） | employeeList（uid/uname/简拼/手机/dept/deptname/employeeRule 角色/move_admin_role/move_Login 移动登录权限/movepassword/仓库绑定 zr_ck/打样工种）+ total | 员工档案与组织归属、角色字段（M19/M20） |
| 部门信息 | `/api/NetYf/Baseinfo/DeptQuery` | — | deptList（id/name/简拼/**pid 上级**/sysdept 本厂/company/companyName/isdelete）+ total | 部门全集与父子关系，组织树/车间对比清单依据 |

### 2.3 生产计划与制单类（4 个）

| 接口 | 地址 | 关键入参 | 返回要点 | 用途 |
| :--- | :--- | :--- | :--- | :--- |
| 生产计划 | `/api/NetYf/Plan/GridPageList` | page、size、dates、datee | dh 单号 / jhdh 计划单号 / khddh 客户订单号 / khid·khname·khhh 客户 / gdy 跟单员 / zsl 总数量 / ddsl 订单数 / paol 抛量 / **sl 预发数量** / finish_date 交货日期 / state 状态(1=审核) / total | 订单/计划的量与交期，交期预警数据源 |
| 生产制单 | `/api/NetYf/Sclzd/GridPageList` | page、size、dates、datee | dh / dddh 计划单号 / khname / huohao / chuanghao·baohao·ganghao / **fhsl 预发 / sssl 产量** / cjr 裁剪人 / drdg_status 导入吊挂 / **id=物料编号** / total | 裁剪/制单环节，在制数量数据源 |
| 生产制单工序 | `/api/NetYf/Sclzd/SclzdWorktypeQuery` | dh | 该制单的工序路线（wt/wtname/sort/zhgx/sfzb/生产类型）+ total | 单据维度工序路线 |
| 生产制单查询工序扫描 | `/api/NetYf/Sclzd/SclzdBarcodeQuery` | dh、detailId（物料编号） | barcodeZb 整版 / barcode 非整版：各工序 uid/uname/dept/worktype/wtname/inputtime + totalZb/total | 查某物料各工序由谁何时完成 |

### 2.4 产量与进度查询类（6 个）

| 接口 | 地址 | 关键入参 | 返回要点 | 用途 |
| :--- | :--- | :--- | :--- | :--- |
| 线下工序产量产能 | `/api/NetYf/Sclzd/BarcodeClQuery` | page、size、dates、datee | 明细：uid/uname/dept/deptname/rq/床包号/货号颜色尺码/工序/fhsl 预发/**sssl 产量**/sl/price/je + total | 线下扫码产量明细（计件工资原始记录之一） |
| 工序产量查询 | `/api/NetYf/Sclzd/HuohaoWtCLQuery` | page、size、queryFooter、dates、datee、**scheme（必填：货号工序/工序）** | 汇总明细：huohao/工序名/sssl 产量 + footer.sl_total | 按「货号×工序」或「工序」维度的产量汇总 |
| 手工账 | `/api/NetYf/PinFeng/GridPageList` | page、size、dates、datee | 明细：dh/zhdate/state/制单人·审核人/uid/uname/dept/huohao/ddh 计划单号/工序/js 件数/sl 数量/**cp 次品**/chuanghao/颜色尺码/price 单价/je + total | 手工记账产量（唯一含次品字段的产量源） |
| 工序进度 | `/api/NetYf/Sclzd/WorktypeProgressQuery` | page、size、**userid（物料编号，必填）**、uid（员工工号，可为空） | 每道工序：fhsl 预发 / zpsl 正品 / wsort 序号 / name 工序名 / **uid/uname/dept/inputtime（非空=已扫码完成）** / cid 条码ID + total | 进度判定核心接口，配合 M6/M18 计算进度% |
| 已扫描查询 | `/api/NetYf/Sclzd/YskQuery` | page、size、dates、datee、**Uid（必填）** | 明细：uid/uname/dept/物料编号/床包号/货号颜色尺码/工序/fhsl 发卡/**sl 产量**/price/je/cid/sffb 分包 + footer（bs_total/sl_total/je_total） | 按员工查已扫码产量/工资，带合计 |
| 未扫描查询 | `/api/NetYf/Sclzd/WskQuery` | page、size、dates、datee | 未扫工序明细：物料编号/床包号/货号颜色尺码/工序/sl 预发数量 + footer（bs_total/sl_total） | 在制/待生产工序，在制数量数据源 |

### 2.5 工资与排名类（2 个）

| 接口 | 地址 | 关键入参 | 返回要点 | 用途 |
| :--- | :--- | :--- | :--- | :--- |
| 工资明细/汇总 | `/api/NetYf/Sclzd/GongziMxQuery` | page、size、queryFooter、dates、datee、Uid、Flag（0扫描日期/1审核日期）、**Type（0扫码/1吊挂/2手工账，逗号分隔）**、**scheme（空=明细；hz/汇总=按汇总查询）** | 明细：type 工资类型（扫码/吊挂/手工账产量）/rq/uid/uname/dept/床包号/货号/工序/ischeck 是否审核/check_time/fhsl/sl/price/je + footer（bs_total/fhsl_total/sl_total/je_total） | 个人工资明细与汇总核心接口，三源合一；je=sl×price（M9/M18） |
| 员工工资排名 | `/api/NetYf/Sclzd/GongziJeOrderQuery` | page、size、queryFooter、dates、datee | list：uid/uname/**dept 人事部门**/bs 包数/je 金额（**已按金额倒序**）+ footer.je_total | 管理端工资排名，位次按返回顺序编号；可按 dept 分组 |

> 注：最新文档参数表中未列出 GongziMxQuery 的 Huohao/Worktype/Color/Chima/Userid/Chuanghao 等过滤参数；如需按货号/工序/颜色等下钻过滤，联调时确认是否仍支持（见第五章 C.14）。

### 2.6 吊挂系统对接类（3 个）

| 接口 | 地址 | 关键入参 | 返回要点 | 用途 |
| :--- | :--- | :--- | :--- | :--- |
| 吊挂中间库 | `/api/NetYf/Dg/GridPageList` | page、size | list：id/dg_type 吊挂公司/dg_name/dg_Server 地址/dg_Database/dg_Uid/dg_Pwd + total | 吊挂系统配置信息 |
| 吊挂组别 | `/api/NetYf/Dg/DgZuGridPageList` | page、size | list：id/dgname 吊挂线/xianhao 线号/zuBieName 组别 + total | 吊挂线/组别字典 |
| 吊挂工序产能 | `/api/NetYf/Dg/DgClQuery` | page、size、dates、datee | 明细：rq 生产时间/dddh 订单号/huohao 颜色尺码/工序/uid·uname/dguid·dguname 吊挂员工/dept/dgName 吊挂线/dgStyleNo 线号/sl/price/je/sfjz 是否结账 + total | 吊挂产量明细（工资 Type=1 的数据源） |

---

## 三、我方取数与计算逻辑（基于以上接口如何支撑智能体功能）

| 智能体功能 | 取数方案 |
| :--- | :--- |
| 员工·个人产量统计 | `GongziMxQuery`（Type=0,1,2 全量，Uid=本人）或 `YskQuery`；**本人工号直接取 token 响应的 `user`（M10）**；按日/月/自定义传 dates/datee |
| 员工·个人工资汇总/明细 | `GongziMxQuery`：scheme 空=明细，"hz"=汇总；应发合计 = je_total（M9，je=sl×price）；日均工资由我方按「合计 ÷ 天数」计算 |
| 员工·收入排名 | ❌ **已取消**：接口只返回本人数据，无法排名（M7） |
| 员工·合格/次品数量 | ⚠️ 仅手工账有 `cp` 次品，扫码/吊挂无次品字段，展示口径待定（第五章 C.5） |
| 管理·订单/款号进度 | `Plan/GridPageList` 定位订单 → `Sclzd/GridPageList`（制单 `dddh` ↔ 计划 `jhdh`）拿到物料 → `WorktypeProgressQuery`（userid=物料编号）查各工序扫码情况 → 进度% = 「uid 非空工序数 / 总工序数」（M6/M18）；当前工序 = 最大已完成 wsort 的下一道 |
| 管理·订单/款号产量 | `GongziMxQuery` / `HuohaoWtCLQuery` 按货号+时间段过滤汇总（制单语境完成数量以 `sssl` 为准，见 1.5 分语境表） |
| 管理·车间产量对比 | 各车间数据由 MES 权限过滤后返回，我方按 dept 分组聚合人均/名次（M16）；车间清单用 `DeptQuery` |
| 管理·员工工资清单/排名 | `GongziJeOrderQuery`（倒序即排名，**含 dept 可按车间分组**）+ `GongziMxQuery` 明细 |
| 老板·全厂总览 | 老板身份调用时 MES 返回全厂数据，我方聚合（M16）；仅在线查询实现，定时类（日报/主动推送等）当前版本不做（K1） |
| 异常高亮（交期预警） | 逾期告警与预警等级**当前版本不做**（K4）；基础的 finish_date 与当前日期对比高亮由我方联调时直接实现，不依赖客户配置 |
| 用户身份与角色展示 | token 响应 `user`/`uname`（JWT 载荷另有 userType）+ `UserInfoQuery`（角色相关字段，枚举值待确认，见第五章 A.1）+ `EmployeeQuery` 的 move_admin_role/employeeRule（M11：只做展示参考，不做权限判断） |

---

## 四、工程约定

| 项目 | 方案 |
| :--- | :--- |
| 凭证刷新 | accessToken 有效期 2 小时（M2）：跳转时前端获取，到期前（如 90 分钟）或遇过期类错误时刷新；sign/timestamp 随每次请求携带，有效期由客户延长，过期后重新调 `/api/system/token` 刷新（M1/M8） |
| 接入形态 | 员工端在客户小程序内以悬浮窗形式集成（M10）；前端获取 token（M15）并把当前用户 accessToken 传给我方后端；后端以该用户 accessToken + token 接口返回的 sign/timestamp 调业务接口（数据按该身份行级过滤，M3） |
| 根地址 | 测试环境 `http://hzlinkbiz.ywhzsoft.com:9002`（客户已确认）；正式环境地址由客户联调前提供（第五章 B.4） |
| 分页 | 传 page/size；列表接口均有 total（M13），累计取到 total 即结束 |
| 错误处理 | 先判 HTTP status code、再看 code/message（M14）：404（接口地址错误，核对根地址与路径）/ app_key 空 / 请求已过期（重新调 token 接口刷新凭证）/ 签名无效（重新调 token 接口刷新）/ 无效 app_key / 加密信息解析失败（用户携带数据有误，引导用户重进）；参数错误、无权限等由我方平台封装友好提示（M12） |
| 数据归档 | 我方侧所有缓存/历史/收藏数据以 AppKey 为租户键存储；用户映射（uid ↔ uname/company）可长期保存（M20） |
| 聚合计算 | 对比/趋势/达成率等聚合由我方拉明细自算（客户允许，M16）；性能上限当前版本不管（K3），缓存策略与延迟容忍度在联调/压测时再定（后期可能做压测） |
| 推送 | **当前版本不做**（一切推送依赖定时任务，定时任务当前版本不做，K1/K6）；MES 无事件/订阅能力；后续方向预留：我方定时轮询 + 对接客户已有消息渠道（M17） |
| 报表导出 | 我方已有报表导出接口，智能体前端能拿到下载接口即可，无需额外处理，无需 MES 提供报表服务（K5） |
| 权限与角色 | 不获取、不判断用户角色（M11/M19）；行级信任 MES 过滤；越权判定 = 返回范围小于请求范围 → 友好提示（M12，文案待定） |

---

## 五、目前仍不清楚的地方（待客户解释）

> 凡已确认的条目均在前四章（M1~M20）；凡已拍板范围（当前版本不做/不管）的条目记入第六章（K1~K7），不列入本章。本章**只保留真正未确认**的问题。
> 标记：🔴 阻断开发 ｜ 🟡 重要（影响方案设计）｜ 🟢 一般（联调后顺带确认即可）。
> 当前共 14 项：🔴 1 ｜ 🟡 5 ｜ 🟢 8。

### A. 身份、权限与组织

1. **🟢 角色展示与角色化快捷问题**（功能点 25/27）：token JWT 载荷含 `userType`（客户示例值「小程序用户」）；`UserInfoQuery` 返回含角色相关字段——如确为用户角色，需客户提供枚举值（正在询问中）；「员工/管理/老板」三级展示映射仍待与客户对齐（或直接采用客户侧角色定义）。

### B. 环境与联调

2. **🔴 测试 app_key 与账号交付**：测试环境根地址已确认 `hzlinkbiz.ywhzsoft.com:9002`；仍需提供：有效测试 app_key（含前端跳转用的加密 app_key）、测试账号、联调计划。（客户问题集「五、请 MES 提供的资料」已列，尚未交付。）
3. **🟢 凭证刷新细节**：accessToken（2 小时）刷新后旧 token 是否立即失效？过期后的重试策略？（08-24 会议遗留。）
4. **🟢 接口地址与正式环境**：27 个接口请确认统一以正式根地址为准（文档部分示例为 localhost 相对路径）；正式环境根地址由客户联调前提供；工序产量查询旧地址 `PostHuohaoWtCLQuery` 是否废弃（现按 `HuohaoWtCLQuery` 执行）。

### C. 业务口径

5. **🟡 合格/次品数量**：只有手工账有 `cp` 次品，扫码/吊挂产量无次品字段——员工端「合格数量/次品数量」如何展示（默认 0？仅手工账口径？）
6. **🟡 「订单」检索口径**：用户说「查一下 XX 订单」时，按 `dh` 单号 / `jhdh` 计划单号 / `khddh` 客户订单号，还是按货号 `bbreed` 或简拼 `name_pk` 找？支持模糊/简拼搜索吗？
7. **🟡 在册人数**：全厂工资汇总需要在职人数，在职/离职状态用什么字段判断（`isdelete`？`move_Login`？）。
8. **🟡 量产状态（试产/量产）**：需求里有该字段，现有接口均无，数据源在哪？
9. **🟡 目标产量/达成率**：目标产量的数据源在哪？计划接口有 `zsl`（总数量）/ `ddsl`（订单数）/ `sl`（预发数量），以哪个为「目标」？能拆到工序吗？
10. **🟢 产量三字段细口径**：文档字段标注已明确多数语境（制单 `sssl`=产量、已扫描 `sl`=产量、`fhsl`=预发/发卡、计划 `sl`=预发数量），客户已确认工资=sl×price；仅「fhsl/sssl 各自在哪个环节写入」未逐字段说明，联调时顺带确认。
11. **🟢 工价特殊计价**：工价维护位置已明确（工序 `default_price` 默认工价 / `xz_price` 限制工价 + 尺码 `price` 单价 + 结算方式 预发/实收/多次实收）；是否存在阶梯价、加成等特殊计价仍待确认。
12. **🟢 时间口径默认值**：`Flag` 0=扫描日期 / 1=审核日期，智能体默认用哪个？
13. **🟢 一人多订单（客户问题集未作答）**：一名员工同时参与多个订单时，MES 返回的数据能否区分其在不同订单中的产量和工资（我方取数原则上可按制单/货号维度区分，联调验证）。
14. **🟢 工资明细过滤参数**：最新文档参数表未列出 GongziMxQuery 的 Huohao/Worktype/Color/Chima/Userid/Chuanghao 等过滤参数，请确认是否仍支持（影响「按工序/按货号看工资」等下钻功能）。

---

## 六、当前版本已知条件与范围决策（后续版本前置知识）

> 以下条目为已确认的**已知条件**（2026-08-25 与客户沟通/我方评估确定），**不属于「未确认项」**；其中部分为「当前版本不做」，后续版本若要做，需先满足各条列出的前置条件。

| # | 已知条件 / 范围决策 | 说明与对后续版本的影响 |
| :--- | :--- | :--- |
| K1 | **定时任务与定时身份：当前版本不做**（已与客户确认） | 目前没有固定的服务账号能通过定时身份获取 Access Token（token 依赖小程序跳转携带的「用户加密 app_key」，需用户在线）。因此定时类功能（全厂日报、主动推送等）当前版本不做；全厂数据仍可通过老板本人在线查询获得。**后续版本前置条件：客户需先提供固定服务账号（或等价的后台获取 token 方式）。** |
| K2 | **组织树：只取最新关系，不存历史** | 我方不存储组织树数据，也不需要调岗、借调等历史组织关系：取数时用 `DeptQuery`（含 `pid`）组装**最新**部门树即可；权限判断由 MES 按用户**当前状态**通过接口完成，我方只依赖用户当前状态。此为我方设计决策，无需客户再确认。 |
| K3 | **性能上限：当前版本不管** | 不作为当前版本阻塞项；后期可能做压测，届时再据此确定缓存策略与数据延迟容忍度。 |
| K4 | **交期预警（逾期与预警等级）：当前版本不做** | 预警天数阈值维护、预警等级划分均不在本期范围；基础的「交货日期 vs 当前日期」对比高亮如需实现，由我方按当前日期直接比较，不依赖客户侧配置。 |
| K5 | **报表导出：我方自有机制** | 我方已有报表导出接口，智能体前端能拿到下载接口即可，无需额外处理，也无需 MES 提供报表生成/下载服务。 |
| K6 | **消息推送与日期跨度限制：当前版本不做/不管** | 一切推送类功能均依赖定时任务（K1，当前版本不做），故推送细节（具体渠道、消息格式、通知范围）本期均不做；日期跨度限制不作为本期阻塞项，前端联调时自定合理跨度上限即可。 |
| K7 | **用户菜单接口（MoveMenuQuery）：当前版本不使用** | 评估后该接口当前版本无明确用途，先忽略；接口仍在客户清单中保留，后续版本如需菜单式入口/角色化入口可直接复用。 |
