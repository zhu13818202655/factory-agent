# AI问答对外接口文档

本文档面向 AI 问答（智能体）平台对接 MES 系统使用。除特别说明外，所有接口均为 `POST` 请求，请求体为 JSON。认证方式与通用响应约定见前两个章节，后续接口说明不再重复。

## 1. 通用说明

### 1.1 请求约定

- 请求方式：`POST`
- Content-Type：`application/json`
- 除认证接口本身外，所有接口的 Header 都必须携带 `Authorization` 认证字段（Bearer Token，见「认证」章节）。
- 除认证接口本身外，所有接口的请求体都必须携带三个认证参数（见「认证」章节），后续接口说明中只列出业务参数，不再重复。

### 1.2 响应约定

所有接口统一返回如下 JSON 结构：

```json
{
    "code": 1,
    "message": "成功",
    "result": {},
    "timestamp": 1787879598
}
```

- `code`：`1` 表示成功，`0` 表示失败
- `message`：结果描述，失败时为具体错误原因
- `result`：业务数据，失败时为 `null`
- `timestamp`：接口返回的秒级时间戳

后文各接口的「响应字段说明」仅描述 `result` 内的业务字段。

### 1.3 状态码说明

| 状态码 | 说明 |
| --- | --- |
| 200 | 成功 |
| 400 | 失败，如参数为空、无效app_key、请求已过期、加密信息解析失败等，具体原因见 `message` |
| 404 | 无登录权限 / 无数据 |

## 2. 认证

接入分两步：

**第一步**：调用「获取认证信息」接口获取认证信息，这是所有请求的前提。

**第二步**：调用其余任何接口时，必须同时携带以下两部分认证信息：

**① Header 认证（Bearer Token）**：在 Header 添加参数 `Authorization`，其值为在 Bearer 之后拼接空格和访问令牌（即认证接口返回的 `accessToken` 字段）。示例：

```
Authorization: Bearer your_access_token
```

**② 请求体认证参数**：在请求体中携带以下三个认证参数：

| 参数名 | 类型 | 必填 | 来源 |
| --- | --- | --- | --- |
| app_key | string | 是 | 认证接口返回的 `appkey` 字段（注意：请求时字段名为 `app_key`） |
| timestamp | integer | 是 | 认证接口返回的 `timestamp` 字段 |
| sign | string | 是 | 认证接口返回的 `sign` 字段 |

携带认证信息后的完整业务请求示例：

```
Header:
Authorization: Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiMDEwMDEi...
```

```json
{
    "app_key": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
    "timestamp": 1787879598,
    "sign": "e401cc7b3b197e410659769f82ec020c",
    "dates": "2026-08-01",
    "datee": "2026-08-14"
}
```

**用户唯一标识**：系统通过 `app_key` + `uid` 唯一标识一个用户，其中 `app_key` 标识应用（访问权限），`uid` 为员工工号（即认证接口返回的 `user` 字段）。需要按指定用户查询数据的接口（如员工信息接口的 `uid`、工资明细接口的 `Uid`），均以此工号传参。

### 2.1 获取认证信息

**接口地址**：`/api/system/token`

**请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| app_key | string | 是 | 用户访问智能体时携带的加密凭证，系统据此换取访问令牌、签名和时间戳 |

**请求示例**

```json
{
    "app_key": "BB/GTvh1GXQ5o9SXC8uvHadfRNoIekgAcItBtvowNePdpbSVsDFNEoxXUF/qU+hPnmr8itELafkhnwx41B0gfzxeqxKDYGe92pyqplxbQOyJ6pg+MqDNxHUYOgSUImEZ"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "tokenType": "Bearer",
        "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiMDEwMDEiLCJ1bmFtZSI6IuadqOWfuuWxsSIsImxvZ2luVXNlck5hbWUiOiIiLCJsb2dpblJlYWxOYW1lIjpudWxsLCJkZXB0IjoiMDAzIiwiY3VzdG9tSWQiOiI3Mzg4QzQ2RS1CNEZFLTQ2RDMtQkY2Qy03NUU4Q0JGNzRFN0MiLCJ1c2VyVHlwZSI6IuWwj-eoi-W6j-eUqOaItyIsInJvbGVzIjoiMDAiLCJpYXQiOjE3ODc4Nzk1OTgsIm5iZiI6MTc4Nzg3OTU5OCwiZXhwIjoxNzg3ODg2Nzk4LCJpc3MiOiJIekR1aUppZVNlcnZlciIsImF1ZCI6Ikh6RHVpSmllU2VydmVyLkFwaUNsaWVudHMifQ.F4I4ekCeUoh40afOgo-6SXTomWPdCwhJZixIVJutWsI",
        "expiresIn": 7200,
        "expiresAt": "2026-08-28T03:13:18.4901174+00:00",
        "user": "01001",
        "uname": "杨基山",
        "dept": "003",
        "loginUserName": "",
        "appkey": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
        "sign": "e401cc7b3b197e410659769f82ec020c",
        "timestamp": 1787879598,
        "roles": "00",
        "permissions": []
    },
    "timestamp": 1787879598
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.tokenType | string | 令牌类型，固定为 Bearer |
| result.accessToken | string | 访问令牌，调用业务接口时用于 Header 的 `Authorization` 字段，有效期为 `expiresIn` 秒 |
| result.expiresIn | integer | 令牌有效期（秒） |
| result.expiresAt | string | 令牌过期时间 |
| result.user | string | 员工工号 |
| result.uname | string | 员工姓名 |
| result.dept | string | 部门编号 |
| result.loginUserName | string | ERP登录用户名 |
| result.appkey | string | 弘兆分配的 app_key，调用业务接口时作为 `app_key` 参数传入 |
| result.sign | string | 请求签名，调用业务接口时作为 `sign` 参数传入 |
| result.timestamp | integer | 秒级时间戳，调用业务接口时作为 `timestamp` 参数传入 |
| result.roles | string | 角色（移动管理岗位）：00 员工 / 01 组长 / 02 管理 / 99 老板 |
| result.permissions | array | 权限列表 |

> 注意：`timestamp` 有有效期（默认 60 秒），若接口报「请求已过期」，请重新调用本接口获取最新认证信息。

### 2.2 测试权限

**接口地址**：`/api/print/test-permissions`

用于验证当前认证信息是否有效，请求体只需携带三个认证参数，无其他业务参数。

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": "调用成功",
    "timestamp": 1786360218
}
```

## 3. 接口总览

| 分类 | 接口 | 接口地址 | 说明 |
| --- | --- | --- | --- |
| 认证 | 获取认证信息 | /api/system/token | 获取 app_key、sign、timestamp，所有请求的前提 |
| 认证 | 测试权限 | /api/print/test-permissions | 验证认证信息是否有效 |
| 基础数据 | 用户信息查询 | /api/NetYf/Baseinfo/UserInfoQuery | 查询登录用户信息 |
| 基础数据 | 用户菜单查询 | /api/NetYf/Baseinfo/MoveMenuQuery | 查询用户移动端菜单 |
| 基础数据 | 货号信息单据接口 | /api/NetYf/Baseinfo/HuohaoQuery | 查询货号及货号类型 |
| 基础数据 | 货号信息表单接口 | /api/NetYf/Baseinfo/HuohaoFormQuery | 按货号查询颜色、尺码明细 |
| 基础数据 | 生产类型接口 | /api/NetYf/Baseinfo/ScTypeQuery | 查询生产类型字典 |
| 基础数据 | 生产工序接口 | /api/NetYf/Baseinfo/RfidWorktypeQuery | 查询生产工序字典 |
| 基础数据 | 货号工序接口 | /api/NetYf/Baseinfo/HuohaoWorktypeQuery | 按货号查询工序 |
| 基础数据 | 员工信息接口 | /api/NetYf/Baseinfo/EmployeeQuery | 查询员工信息 |
| 基础数据 | 部门信息接口 | /api/NetYf/Baseinfo/DeptQuery | 查询部门信息 |
| 生产计划 | 生产计划接口 | /api/NetYf/Plan/GridPageList | 按日期范围分页查询生产计划 |
| 生产制单 | 生产制单接口 | /api/NetYf/Sclzd/GridPageList | 按日期范围分页查询生产制单 |
| 生产制单 | 生产制单工序接口 | /api/NetYf/Sclzd/SclzdWorktypeQuery | 按单号查询制单工序 |
| 生产制单 | 生产制单工序扫描接口 | /api/NetYf/Sclzd/SclzdBarcodeQuery | 按单号和物料编号查询扫描记录 |
| 产量与产能 | 线下工序产量产能 | /api/NetYf/Sclzd/BarcodeClQuery | 分页查询线下扫码产量明细 |
| 产量与产能 | 工序产量查询接口 | /api/NetYf/Sclzd/HuohaoWtCLQuery | 按货号工序/工序汇总查询产量 |
| 产量与产能 | 工序进度查询接口 | /api/NetYf/Sclzd/WorktypeProgressQuery | 按物料编号查询各工序进度 |
| 工资 | 工资明细/汇总查询接口 | /api/NetYf/Sclzd/GongziMxQuery | 查询工资明细或汇总（线下+吊挂+手工账） |
| 工资 | 员工工资排名查询接口 | /api/NetYf/Sclzd/GongziJeOrderQuery | 按日期范围查询员工工资排名 |
| 生产查询 | 生产查询-已扫描接口 | /api/NetYf/Sclzd/YskQuery | 分页查询已扫描产量记录 |
| 生产查询 | 生产查询-未扫描接口 | /api/NetYf/Sclzd/WskQuery | 分页查询未扫描产量记录 |
| 手工账 | 手工账接口 | /api/NetYf/PinFeng/GridPageList | 分页查询手工账单据 |
| 吊挂对接 | 吊挂对接中间库接口 | /api/NetYf/Dg/GridPageList | 查询吊挂中间库连接配置 |
| 吊挂对接 | 吊挂组别接口 | /api/NetYf/Dg/DgZuGridPageList | 查询吊挂线别与组别 |
| 吊挂对接 | 吊挂工序产能 | /api/NetYf/Dg/DgClQuery | 分页查询吊挂产量明细 |

## 4. 基础数据

### 4.1 用户信息查询接口

**接口地址**：`/api/NetYf/Baseinfo/UserInfoQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| USERNAME | string | 是 | 登录用户 |

**请求示例**

```json
{
    "USERNAME": "admin"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "code": "Admin",
                "username": "Admin",
                "realname": "管理员",
                "companyName": "有缝H6内销版测试",
                "roles": "01"
            }
        ],
        "total": 1
    },
    "timestamp": 1787554551
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 用户信息列表 |
| result.list.code | string | 用户编码 |
| result.list.username | string | 用户名 |
| result.list.realname | string | 姓名 |
| result.list.companyName | string | 公司名称 |
| result.list.roles | string | 角色（移动管理岗位）：00 员工 / 01 组长 / 02 管理 / 99 老板 |
| result.total | integer | 数据总数量 |

### 4.2 用户菜单查询接口

**接口地址**：`/api/NetYf/Baseinfo/MoveMenuQuery`

**业务请求参数**：无（仅需携带认证参数）

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "uid": "004",
                "uname": "庞保忠",
                "dept": "",
                "menus": [
                    {
                        "name": "扫码计件",
                        "model": "工作台",
                        "isScan": true,
                        "sort": "10101"
                    }
                ]
            }
        ],
        "total": 1
    },
    "timestamp": 1787565615
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 用户菜单列表 |
| result.list.uid | string | 员工工号 |
| result.list.uname | string | 姓名 |
| result.list.dept | string | 部门编号 |
| result.list.menus | array | 菜单列表 |
| result.list.menus.name | string | 菜单名称 |
| result.list.menus.model | string | 菜单模块 |
| result.list.menus.isScan | boolean | 进入时是否调用扫描 |
| result.list.menus.sort | string | 排序号 |
| result.total | integer | 数据总数量 |

### 4.3 货号信息单据接口

**接口地址**：`/api/NetYf/Baseinfo/HuohaoQuery`

**业务请求参数**：无（仅需携带认证参数）

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "huohaoList": [
            {
                "bh": "00001",
                "bbreed": "911",
                "name_pk": "911",
                "description": "女士长袖",
                "stype": "0001",
                "huohaotype": "衣服",
                "dw": "件",
                "lpinpai": "",
                "isdelete": 0,
                "jst_huohao": "911"
            }
        ],
        "hh_total": 1,
        "huohaoTypeList": [
            {
                "id": 1,
                "bh": "0001",
                "pbh": "#",
                "name": "衣服",
                "name_pk": "yf",
                "isdelete": 0
            }
        ],
        "ht_total": 1
    },
    "timestamp": 1786540713
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.huohaoList | array | 货号信息集合 |
| result.huohaoList.bh | string | 货号编码 |
| result.huohaoList.bbreed | string | 货号 |
| result.huohaoList.name_pk | string | 货号简拼 |
| result.huohaoList.description | string | 品名 |
| result.huohaoList.stype | string | 货号类型编码 |
| result.huohaoList.huohaotype | string | 货号类型 |
| result.huohaoList.dw | string | 单位 |
| result.huohaoList.lpinpai | string | 品牌 |
| result.huohaoList.isdelete | integer | 是否删除 |
| result.huohaoList.jst_huohao | string | 聚水潭货号 |
| result.hh_total | integer | 货号信息总数 |
| result.huohaoTypeList | array | 货号类型集合 |
| result.huohaoTypeList.id | integer | ID |
| result.huohaoTypeList.bh | string | 货号类型编码 |
| result.huohaoTypeList.pbh | string | 根目录编号 |
| result.huohaoTypeList.name | string | 货号类型 |
| result.huohaoTypeList.name_pk | string | 货号类型简拼 |
| result.huohaoTypeList.isdelete | integer | 是否删除 |
| result.ht_total | integer | 货号类型总数 |

### 4.4 货号信息表单接口

**接口地址**：`/api/NetYf/Baseinfo/HuohaoFormQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| huohao | string | 是 | 货号编号 |

**请求示例**

```json
{
    "huohao": "00001"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "huohaoList": [
            {
                "bh": "00001",
                "bbreed": "911",
                "name_pk": "911",
                "description": "女士长袖",
                "stype": "0001",
                "huohaotype": "衣服",
                "dw": "件",
                "lpinpai": "",
                "isdelete": 0,
                "jst_huohao": "911"
            }
        ],
        "hh_total": 1,
        "huohaoColorList": [
            {
                "id": 1,
                "bh": "00001",
                "color": "红色",
                "uploadguid": "98A6CC9A-C3A9-8776-B232-D7FD027ECD6E"
            }
        ],
        "hc_total": 1,
        "huohaoChimaList": [
            {
                "id": 1,
                "bh": "00001",
                "chima": "S",
                "banx": null,
                "kez": null,
                "xs_price": 0,
                "price": 0
            }
        ],
        "hs_total": 1
    },
    "timestamp": 1786544009
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.huohaoList | array | 货号信息集合 |
| result.huohaoList.bh | string | 货号编号 |
| result.huohaoList.bbreed | string | 货号名称 |
| result.huohaoList.name_pk | string | 货号简拼 |
| result.huohaoList.description | string | 品名 |
| result.huohaoList.stype | string | 货号类型编码 |
| result.huohaoList.huohaotype | string | 货号类型 |
| result.huohaoList.dw | string | 单位 |
| result.huohaoList.lpinpai | string | 品牌编号 |
| result.huohaoList.isdelete | integer | 是否删除 |
| result.huohaoList.jst_huohao | string | 聚水潭货号 |
| result.hh_total | integer | 货号信息总数 |
| result.huohaoColorList | array | 货号颜色集合 |
| result.huohaoColorList.id | integer | ID |
| result.huohaoColorList.bh | string | 货号编号 |
| result.huohaoColorList.color | string | 颜色 |
| result.huohaoColorList.uploadguid | string | 图片guid |
| result.hc_total | integer | 货号颜色总数 |
| result.huohaoChimaList | array | 货号尺码集合 |
| result.huohaoChimaList.id | integer | ID |
| result.huohaoChimaList.bh | string | 货号编号 |
| result.huohaoChimaList.chima | string | 尺码 |
| result.huohaoChimaList.banx | string | 版型 |
| result.huohaoChimaList.kez | string | 克重 |
| result.huohaoChimaList.xs_price | number | 销售价 |
| result.huohaoChimaList.price | number | 单价 |
| result.hs_total | integer | 货号尺码总数 |

### 4.5 生产类型接口

**接口地址**：`/api/NetYf/Baseinfo/ScTypeQuery`

**业务请求参数**：无（仅需携带认证参数）

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "sctypeList": [
            {
                "bh": "0001",
                "name": "整件",
                "name_pk": "zj",
                "sfjcj": 1,
                "isdelete": 0,
                "sfcprk": 0
            }
        ],
        "total": 1
    },
    "timestamp": 1786696855
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.sctypeList | array | 生产类型集合 |
| result.sctypeList.bh | string | 生产类型编号 |
| result.sctypeList.name | string | 生产类型名称 |
| result.sctypeList.name_pk | string | 简拼 |
| result.sctypeList.sfjcj | integer | 是否计裁剪 |
| result.sctypeList.isdelete | integer | 是否删除 |
| result.sctypeList.sfcprk | integer | 是否成品入库 |
| result.total | integer | 数据总数量 |

### 4.6 生产工序接口

**接口地址**：`/api/NetYf/Baseinfo/RfidWorktypeQuery`

**业务请求参数**：无（仅需携带认证参数）

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "worktypeList": [
            {
                "bh": "0001",
                "name": "裁剪",
                "name_pk": "cj",
                "gxtype": 0,
                "isdelete": 0,
                "section": "前道",
                "jc": null,
                "sc_type": null,
                "worktype_group": null,
                "yfgs": 0,
                "default_price": null,
                "gongzi_js_type": 1,
                "wt_sort": null,
                "xz_price": null,
                "default_working_hours": 42,
                "vehicle_type": "平车"
            }
        ],
        "total": 1
    },
    "timestamp": 1786696978
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.worktypeList | array | 生产工序集合 |
| result.worktypeList.bh | string | 编号 |
| result.worktypeList.name | string | 工序 |
| result.worktypeList.name_pk | string | 简码 |
| result.worktypeList.gxtype | integer | 流转类型（0 工序流转 / 1 仓库流转 / 2 验布工资专用） |
| result.worktypeList.isdelete | integer | 是否删除 |
| result.worktypeList.section | string | 工段 |
| result.worktypeList.jc | string | 简称 |
| result.worktypeList.sc_type | string | 系统工序 |
| result.worktypeList.worktype_group | string | 工序组 |
| result.worktypeList.yfgs | integer | 预发改数 |
| result.worktypeList.default_price | number | 默认工价 |
| result.worktypeList.gongzi_js_type | integer | 工资结算方式（0 预发 / 1 实收 / 2 多次实收） |
| result.worktypeList.wt_sort | string | 工序号 |
| result.worktypeList.xz_price | number | 限制工价 |
| result.worktypeList.default_working_hours | number | 默认理论工时 |
| result.worktypeList.vehicle_type | string | 车种 |
| result.total | integer | 数据总数量 |

### 4.7 货号工序接口

**接口地址**：`/api/NetYf/Baseinfo/HuohaoWorktypeQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| huohao | string | 是 | 货号编号 |

**请求示例**

```json
{
    "huohao": "00001"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "id": 3,
                "huohao": "00001",
                "huohaoname": "25-MMT40218F第8-1单",
                "wt": "0001",
                "wtname": "验布",
                "sort": 1,
                "sctype": "0002",
                "sctypename": "大身",
                "sfzb": 0,
                "using_state": 1,
                "zhgx": 0,
                "sfxs": 0,
                "theoretical_work_hours": 42
            }
        ],
        "total": 1
    },
    "timestamp": 1786950207
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 货号工序列表 |
| result.list.id | integer | ID |
| result.list.huohao | string | 货号编号 |
| result.list.huohaoname | string | 货号名称 |
| result.list.wt | string | 工序编号 |
| result.list.wtname | string | 工序名称 |
| result.list.sort | integer | 序号 |
| result.list.sctype | string | 生产类型编号 |
| result.list.sctypename | string | 生产类型 |
| result.list.sfzb | integer | 是否整版（0 否 / 1 是） |
| result.list.using_state | integer | 使用中（0 否 / 1 是） |
| result.list.zhgx | integer | 最后工序（0 否 / 1 是） |
| result.list.sfxs | integer | 是否线上（0 否 / 1 是） |
| result.list.theoretical_work_hours | number | 理论工时 |
| result.total | integer | 数据总数量 |

### 4.8 员工信息接口

**接口地址**：`/api/NetYf/Baseinfo/EmployeeQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| uid | string | 否 | 员工工号 |

**请求示例**

```json
{
    "uid": "01001"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "employeeList": [
            {
                "uid": "01001",
                "uname": "杨基山",
                "name_pk": "yjs",
                "move_Login": 1,
                "dept": "003",
                "deptname": "裁剪车间",
                "move_scan": 0,
                "loginUserName": "",
                "zr_ck": "",
                "dy_gongzhong": "",
                "roles": "00"
            }
        ],
        "total": 1
    },
    "timestamp": 1786697009
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.employeeList | array | 员工集合 |
| result.employeeList.uid | string | 员工工号 |
| result.employeeList.uname | string | 员工姓名 |
| result.employeeList.name_pk | string | 简码 |
| result.employeeList.mobile | string | 手机号 |
| result.employeeList.move_Login | integer | 移动登录权限 |
| result.employeeList.dept | string | 所属部门编号 |
| result.employeeList.deptname | string | 所属部门名称 |
| result.employeeList.move_scan | integer | 移动扫描方式（0 绑定工序 / 1 选择工序） |
| result.employeeList.loginUserName | string | 登录用户绑定 |
| result.employeeList.zr_ck | string | 仓库绑定 |
| result.employeeList.dy_gongzhong | string | 打样工种 |
| result.employeeList.roles | string | 角色（移动管理岗位）：00 员工 / 01 组长 / 02 管理 / 99 老板 |
| result.total | integer | 数据总数量 |

### 4.9 部门信息接口

**接口地址**：`/api/NetYf/Baseinfo/DeptQuery`

**业务请求参数**：无（仅需携带认证参数）

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "deptList": [
            {
                "id": "001",
                "name": "研发部",
                "remark": null,
                "name_pk": "yfb",
                "isdelete": 0,
                "sysdept": "本厂",
                "company": "0001",
                "companyName": "宇鹏",
                "pid": "01"
            },
            {
                "id": "002",
                "name": "财务部",
                "remark": null,
                "name_pk": "cwb",
                "isdelete": 0,
                "sysdept": "本厂",
                "company": "0001",
                "companyName": "宇鹏",
                "pid": "01"
            }
        ],
        "total": 2
    },
    "timestamp": 1786696978
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.deptList | array | 部门集合 |
| result.deptList.id | string | 部门编号 |
| result.deptList.name | string | 部门名称 |
| result.deptList.remark | string | 备注 |
| result.deptList.name_pk | string | 简拼 |
| result.deptList.isdelete | integer | 是否删除 |
| result.deptList.sysdept | string | 系统部门 |
| result.deptList.company | string | 公司编码 |
| result.deptList.companyName | string | 公司名称 |
| result.deptList.pid | string | 上级编号 |
| result.total | integer | 数据总数量 |

## 5. 生产计划

### 5.1 生产计划接口

**接口地址**：`/api/NetYf/Plan/GridPageList`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| dates | string | 是 | 开始日期 |
| datee | string | 是 | 结束日期 |

**请求示例**

```json
{
    "page": 1,
    "size": 50,
    "dates": "2026-08-01",
    "datee": "2026-08-14"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "dh": "jh20260810-004",
                "zhdate": "2026-08-10",
                "finish_date": "2026-08-15",
                "jhdh": "26xiyin8-7",
                "hth": null,
                "gdy": "顾颖",
                "zdr": "顾颖",
                "zsl": 100,
                "zdr_sh": "代田田",
                "state": 1,
                "id": "94CD0DAE-141D-4384-BB9E-6EF51A2FBD99",
                "khddh": "26xiyin8-7",
                "pinpai": "",
                "pinpainame": null,
                "khid": "00162",
                "khname": "SHEIN",
                "khhh": "",
                "huohao": "00374",
                "huohaoname": "XY008",
                "spname": "长裤",
                "color": "黑色印花",
                "chima": "S",
                "dw": "条",
                "ddsl": 20,
                "paol": null,
                "sl": 20,
                "remark": null
            }
        ],
        "total": 1
    },
    "timestamp": 1786949808
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 生产计划列表 |
| result.list.dh | string | 单号 |
| result.list.zhdate | string | 下单日期 |
| result.list.finish_date | string | 交货日期 |
| result.list.jhdh | string | 计划单号 |
| result.list.hth | string | 合同号 |
| result.list.gdy | string | 跟单员 |
| result.list.zdr | string | 制单人 |
| result.list.zsl | integer | 总数量 |
| result.list.zdr_sh | string | 审核人 |
| result.list.state | integer | 单据状态（1 审核） |
| result.list.id | string | ID |
| result.list.khddh | string | 客户订单号 |
| result.list.pinpai | string | 品牌编号 |
| result.list.pinpainame | string | 品牌 |
| result.list.khid | string | 客户编号 |
| result.list.khname | string | 客户 |
| result.list.khhh | string | 客户货号 |
| result.list.huohao | string | 货号编号 |
| result.list.huohaoname | string | 货号名称 |
| result.list.spname | string | 品名 |
| result.list.color | string | 颜色 |
| result.list.chima | string | 尺码 |
| result.list.dw | string | 单位 |
| result.list.ddsl | integer | 订单数量 |
| result.list.paol | number | 抛量 |
| result.list.sl | integer | 预发数量 |
| result.list.remark | string | 备注 |
| result.total | integer | 数据总数量 |

## 6. 生产制单

### 6.1 生产制单接口

**接口地址**：`/api/NetYf/Sclzd/GridPageList`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| dates | string | 是 | 开始日期 |
| datee | string | 是 | 结束日期 |

**请求示例**

```json
{
    "page": 1,
    "size": 50,
    "dates": "2026-08-01",
    "datee": "2026-08-14"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "dh": "202608120003",
                "zhdate": "2026-08-12",
                "dddh": "260812-001",
                "khid": "00005",
                "khname": "KY服饰演示",
                "drdg_status": 0,
                "huohao": "00001",
                "huohaoname": "911",
                "description": "女士长袖",
                "sctype": "0001",
                "sctypename": "整件",
                "chuanghao": "3",
                "cjr": "",
                "zdr": "管理员",
                "state": 1,
                "id": 9,
                "baohao": "1",
                "ganghao": "",
                "color": "红色",
                "chima": "S",
                "fhsl": 20,
                "sssl": 18,
                "remark": null
            }
        ],
        "total": 1
    },
    "timestamp": 1786949767
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 生产制单列表 |
| result.list.dh | string | 单号 |
| result.list.zhdate | string | 制单日期 |
| result.list.dddh | string | 订单单号 |
| result.list.khid | string | 客户编号 |
| result.list.khname | string | 客户 |
| result.list.drdg_status | integer | 导入吊挂（0 否 / 1 是） |
| result.list.huohao | string | 货号编号 |
| result.list.huohaoname | string | 货号名称 |
| result.list.description | string | 品名 |
| result.list.sctype | string | 生产类型编号 |
| result.list.sctypename | string | 生产类型 |
| result.list.chuanghao | string | 床号 |
| result.list.cjr | string | 裁剪人 |
| result.list.zdr | string | 制单人 |
| result.list.state | integer | 状态 |
| result.list.id | integer | 物料编号 |
| result.list.baohao | string | 包号 |
| result.list.ganghao | string | 缸号 |
| result.list.color | string | 颜色 |
| result.list.chima | string | 尺码 |
| result.list.fhsl | integer | 预发数量 |
| result.list.sssl | integer | 产量 |
| result.list.remark | string | 备注 |
| result.total | integer | 数据总数量 |

### 6.2 生产制单工序接口

**接口地址**：`/api/NetYf/Sclzd/SclzdWorktypeQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| dh | string | 是 | 单号 |

**请求示例**

```json
{
    "dh": "202608010001"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "id": 58448,
                "dh": "202608010001",
                "huohao": "00092",
                "huohaoname": "MC24395MI",
                "wt": "0001",
                "wtname": "验布",
                "sort": 1,
                "zhgx": 0,
                "sfzb": 0,
                "sctype": "0001",
                "sctypename": "整件"
            },
            {
                "id": 58449,
                "dh": "202608010001",
                "huohao": "00092",
                "huohaoname": "MC24395MI",
                "wt": "0002",
                "wtname": "拉布",
                "sort": 2,
                "zhgx": 0,
                "sfzb": 0,
                "sctype": "0001",
                "sctypename": "整件"
            }
        ],
        "total": 5
    },
    "timestamp": 1786950485
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 制单工序列表 |
| result.list.id | integer | ID |
| result.list.dh | string | 单号 |
| result.list.huohao | string | 货号编号 |
| result.list.huohaoname | string | 货号名称 |
| result.list.wt | string | 工序编号 |
| result.list.wtname | string | 工序名称 |
| result.list.sort | integer | 序号 |
| result.list.zhgx | integer | 最后工序（0 否 / 1 是） |
| result.list.sfzb | integer | 是否整版（0 否 / 1 是） |
| result.list.sctype | string | 生产类型编号 |
| result.list.sctypename | string | 生产类型 |
| result.total | integer | 数据总数量 |

### 6.3 生产制单工序扫描接口

**接口地址**：`/api/NetYf/Sclzd/SclzdBarcodeQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| dh | string | 是 | 单号 |
| detailId | integer | 是 | 物料编号 |

**请求示例**

```json
{
    "dh": "202608160017",
    "detailId": 95715
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "barcodeZb": [
            {
                "uid": "15046",
                "uname": "隋鑫博",
                "dept": "003",
                "worktype": "0003",
                "wtname": "裁剪",
                "inputtime": "2026-08-17 16:01:30"
            }
        ],
        "totalZb": 1,
        "barcode": [
            {
                "uid": "15027",
                "uname": "孙小件",
                "dept": "003",
                "worktype": "0001",
                "wtname": "验布",
                "inputtime": "2026-08-16 23:07:51"
            },
            {
                "uid": "15046",
                "uname": "隋鑫博",
                "dept": "003",
                "worktype": "0002",
                "wtname": "拉布",
                "inputtime": "2026-08-16 23:07:51"
            }
        ],
        "total": 4
    },
    "timestamp": 1786953699
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.barcodeZb | array | 整版信息 |
| result.barcodeZb.uid | string | 员工工号 |
| result.barcodeZb.uname | string | 员工 |
| result.barcodeZb.dept | string | 部门编号 |
| result.barcodeZb.worktype | string | 工序编号 |
| result.barcodeZb.wtname | string | 工序名称 |
| result.barcodeZb.inputtime | string | 扫描时间 |
| result.totalZb | integer | 整版信息总数 |
| result.barcode | array | 非整版信息 |
| result.barcode.uid | string | 员工工号 |
| result.barcode.uname | string | 员工 |
| result.barcode.dept | string | 部门编号 |
| result.barcode.worktype | string | 工序编号 |
| result.barcode.wtname | string | 工序名称 |
| result.barcode.inputtime | string | 扫描时间 |
| result.total | integer | 数据总数量 |

## 7. 产量与产能

### 7.1 线下工序产量产能

**接口地址**：`/api/NetYf/Sclzd/BarcodeClQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| dates | string | 是 | 开始日期 |
| datee | string | 是 | 结束日期 |

**请求示例**

```json
{
    "page": 1,
    "size": 50,
    "dates": "2026-07-01",
    "datee": "2026-08-14"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "inputtime": "2026-08-12T22:07:56.17",
                "uid": "88888",
                "uname": "演示员工",
                "dept": "001",
                "deptname": "缝制车间",
                "rq": "2026-08-12",
                "chuanghao": "2",
                "sctype": "0001",
                "sctypename": "整件",
                "baohao": "1",
                "id": 5,
                "huohao": "00001",
                "bbreed": "911",
                "description": "女士长袖",
                "color": "红色",
                "chima": "S",
                "worktype": "0001",
                "wtname": "裁剪",
                "fhsl": 50,
                "sssl": 50,
                "sl": 50,
                "price": 1,
                "je": 50
            }
        ],
        "total": 1
    },
    "timestamp": 1786773213
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 产量明细列表 |
| result.list.inputtime | string | 刷卡时间 |
| result.list.uid | string | 员工工号 |
| result.list.uname | string | 员工姓名 |
| result.list.dept | string | 人事部门编号 |
| result.list.deptname | string | 人事部门名称 |
| result.list.rq | string | 制单日期 |
| result.list.chuanghao | string | 床号 |
| result.list.sctype | string | 生产类型编号 |
| result.list.sctypename | string | 生产类型名称 |
| result.list.baohao | string | 包号 |
| result.list.id | integer | 物料编号 |
| result.list.huohao | string | 货号编号 |
| result.list.bbreed | string | 货号名称 |
| result.list.description | string | 品名 |
| result.list.color | string | 颜色 |
| result.list.chima | string | 尺码 |
| result.list.worktype | string | 工序编号 |
| result.list.wtname | string | 工序名称 |
| result.list.fhsl | integer | 预发数量 |
| result.list.sssl | integer | 产量 |
| result.list.sl | integer | 预发数量 |
| result.list.price | number | 工价 |
| result.list.je | number | 金额 |
| result.total | integer | 数据总数量 |

### 7.2 工序产量查询接口

**接口地址**：`/api/NetYf/Sclzd/HuohaoWtCLQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| queryFooter | boolean | 是 | 是否返回合计 |
| dates | string | 是 | 开始日期 |
| datee | string | 是 | 结束日期 |
| scheme | string | 是 | 汇总方式：货号工序 / 工序 |

**请求示例**

```json
{
    "page": 1,
    "size": 50,
    "queryFooter": true,
    "dates": "2026-08-01",
    "datee": "2026-08-14",
    "scheme": "货号工序"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "huohao": "724-001",
                "sssl": 1000,
                "worktype": "包装（测试）"
            },
            {
                "huohao": "724-001",
                "sssl": 1000,
                "worktype": "裁剪"
            }
        ],
        "total": 2,
        "footer": {
            "sl_total": 9511
        }
    },
    "timestamp": 1786773213
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 产量汇总列表 |
| result.list.huohao | string | 货号 |
| result.list.sssl | integer | 产量 |
| result.list.worktype | string | 工序 |
| result.total | integer | 数据总数量 |
| result.footer | object | 合计（queryFooter 为 true 时返回） |
| result.footer.sl_total | integer | 产量总数 |

### 7.3 工序进度查询接口

**接口地址**：`/api/NetYf/Sclzd/WorktypeProgressQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| userid | integer | 是 | 物料编号 |
| uid | string | 否 | 员工工号 |

**请求示例**

```json
{
    "page": 1,
    "size": 50,
    "userid": "4496",
    "uid": ""
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "userid": 4496,
                "huohao": "25-CY22050MI第9-1单",
                "color": "深紫风暴印花",
                "chima": "S",
                "baohao": "1",
                "chuanghao": "11",
                "fhsl": 11,
                "worktype": "0036",
                "name": "烫里腰标",
                "uid": "14007",
                "uname": "杨再军",
                "dept": "008",
                "inputtime": "2025-10-17T08:36:49.267",
                "cid": 11860,
                "zpsl": 11,
                "wsort": 5
            },
            {
                "userid": 4496,
                "huohao": "25-CY22050MI第9-1单",
                "color": "深紫风暴印花",
                "chima": "S",
                "baohao": "1",
                "chuanghao": "11",
                "fhsl": 11,
                "worktype": "0037",
                "name": "四六拼腰内外",
                "uid": "02003",
                "uname": "邓要要",
                "dept": "005",
                "inputtime": "2025-09-12T17:28:57.2",
                "cid": 11861,
                "zpsl": 11,
                "wsort": 6
            }
        ],
        "total": 4
    },
    "timestamp": 1787539860
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 工序进度列表 |
| result.list.userid | integer | 物料编号 |
| result.list.huohao | string | 货号 |
| result.list.color | string | 颜色 |
| result.list.chima | string | 尺码 |
| result.list.baohao | string | 包号 |
| result.list.chuanghao | string | 床号 |
| result.list.fhsl | integer | 预发数量 |
| result.list.worktype | string | 工序编号 |
| result.list.name | string | 工序名称 |
| result.list.uid | string | 员工工号 |
| result.list.uname | string | 员工姓名 |
| result.list.dept | string | 部门编号 |
| result.list.inputtime | string | 刷卡时间 |
| result.list.cid | integer | ID |
| result.list.zpsl | integer | 正品数量 |
| result.list.wsort | integer | 排序 |
| result.total | integer | 数据总数量 |

## 8. 工资

### 8.1 工资明细/汇总查询接口（线下+吊挂+手工账）

**接口地址**：`/api/NetYf/Sclzd/GongziMxQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| queryFooter | boolean | 是 | 是否返回合计 |
| dates | string | 是 | 开始日期 |
| datee | string | 是 | 结束日期 |
| Uid | string | 是 | 员工工号 |
| Flag | string | 是 | 日期口径：0 按扫描日期 / 1 按审核日期 |
| Type | string | 是 | 工资类型：0 扫码产量 / 1 吊挂产量 / 2 手工账产量，可多选，如 "0,1,2" |
| scheme | string | 是 | 为空时按明细查询，有值（汇总 / hz / HZ）时按汇总查询 |

**请求示例**

```json
{
    "page": 1,
    "size": 50,
    "queryFooter": true,
    "dates": "2026-08-01",
    "datee": "2026-08-22",
    "Uid": "001",
    "Flag": "0",
    "Type": "0,1,2",
    "scheme": ""
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "id": 10263,
                "type": "手工账产量",
                "rq": "2026-08-21T00:00:00",
                "inputtime": "08-21 00:00:00",
                "uid": "001",
                "uname": "马师傅",
                "dept": "001",
                "chuanghao": "",
                "baohao": "0",
                "huohao": "25-CY22050MI第9-1单",
                "color": "",
                "chima": "",
                "worktype": "拉布",
                "ischeck": 1,
                "check_time": "08-21 00:00:00",
                "fhsl": 333,
                "sl": 333,
                "price": 0.065,
                "je": 21.645,
                "inputtime_raw": null,
                "check_time_raw": null
            }
        ],
        "total": 1,
        "footer": {
            "bs_total": 22,
            "fhsl_total": 7818,
            "sl_total": 7813,
            "je_total": 2111.885
        }
    },
    "timestamp": 1786773213
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 工资明细列表 |
| result.list.id | integer | 物料编号 |
| result.list.type | string | 工资类型 |
| result.list.rq | string | 制单日期 |
| result.list.inputtime | string | 刷卡时间 |
| result.list.uid | string | 员工工号 |
| result.list.uname | string | 员工姓名 |
| result.list.dept | string | 部门编号 |
| result.list.chuanghao | string | 床号 |
| result.list.baohao | string | 包号 |
| result.list.huohao | string | 货号 |
| result.list.color | string | 颜色 |
| result.list.chima | string | 尺码 |
| result.list.worktype | string | 工序 |
| result.list.ischeck | integer | 是否审核 |
| result.list.check_time | string | 审核时间 |
| result.list.fhsl | integer | 预发数量 |
| result.list.sl | integer | 产量 |
| result.list.price | number | 工价 |
| result.list.je | number | 金额 |
| result.list.inputtime_raw | string | 刷卡时间原始值 |
| result.list.check_time_raw | string | 审核时间原始值 |
| result.total | integer | 数据总数量 |
| result.footer | object | 合计（queryFooter 为 true 时返回） |
| result.footer.bs_total | integer | 包数 |
| result.footer.fhsl_total | integer | 预发数量总数 |
| result.footer.sl_total | integer | 产量总数 |
| result.footer.je_total | number | 金额总数 |

### 8.2 员工工资排名查询接口

**接口地址**：`/api/NetYf/Sclzd/GongziJeOrderQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| queryFooter | boolean | 是 | 是否返回合计 |
| dates | string | 是 | 开始日期 |
| datee | string | 是 | 结束日期 |

**请求示例**

```json
{
    "page": 1,
    "size": 50,
    "queryFooter": true,
    "dates": "2026-08-01",
    "datee": "2026-08-14"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "uid": "04061",
                "uname": "王芝兰",
                "dept": "007",
                "bs": 548,
                "je": 6660.52
            }
        ],
        "total": 1,
        "footer": {
            "je_total": 12100
        }
    },
    "timestamp": 1787537015
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 工资排名列表 |
| result.list.uid | string | 员工工号 |
| result.list.uname | string | 员工姓名 |
| result.list.dept | string | 人事部门 |
| result.list.bs | integer | 包数 |
| result.list.je | number | 金额 |
| result.total | integer | 数据总数量 |
| result.footer | object | 合计（queryFooter 为 true 时返回） |
| result.footer.je_total | number | 金额总数 |

## 9. 生产查询

### 9.1 生产查询-已扫描接口

**接口地址**：`/api/NetYf/Sclzd/YskQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| dates | string | 是 | 开始日期 |
| datee | string | 是 | 结束日期 |
| Uid | string | 是 | 员工工号 |

**请求示例**

```json
{
    "page": 1,
    "size": 20,
    "dates": "2026-08-01",
    "datee": "2026-08-22",
    "Uid": "001"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "inputtime": "2026-08-04 17:29:10",
                "inputtime_raw": null,
                "uname": "马师傅",
                "uid": "001",
                "dept": "001",
                "id": 44791,
                "chuanghao": "1090",
                "baohao": "3",
                "huohao": "724-001",
                "color": "雀灰豹纹印花",
                "chima": "L",
                "worktype": "包装（测试）",
                "fhsl": 1000,
                "sl": 1000,
                "price": 0,
                "je": 0,
                "cid": 236519,
                "sffb": 0,
                "fbid": 236519
            }
        ],
        "total": 1,
        "footer": {
            "bs_total": 13,
            "sl_total": 7405,
            "je_total": 1400
        }
    },
    "timestamp": 1787536722
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 已扫描记录列表 |
| result.list.inputtime | string | 扫描时间 |
| result.list.inputtime_raw | string | 扫描时间原始值 |
| result.list.uname | string | 员工姓名 |
| result.list.uid | string | 员工工号 |
| result.list.dept | string | 部门编号 |
| result.list.id | integer | 物料编号 |
| result.list.chuanghao | string | 床号 |
| result.list.baohao | string | 包号 |
| result.list.huohao | string | 货号 |
| result.list.color | string | 颜色 |
| result.list.chima | string | 尺码 |
| result.list.worktype | string | 工序 |
| result.list.fhsl | integer | 发卡数量 |
| result.list.sl | integer | 产量 |
| result.list.price | number | 工价 |
| result.list.je | number | 金额 |
| result.list.cid | integer | ID |
| result.list.sffb | integer | 是否分包 |
| result.list.fbid | integer | 分包ID |
| result.total | integer | 数据总数量 |
| result.footer | object | 合计 |
| result.footer.bs_total | integer | 包数 |
| result.footer.sl_total | integer | 产量总数 |
| result.footer.je_total | number | 金额总数 |

### 9.2 生产查询-未扫描接口

**接口地址**：`/api/NetYf/Sclzd/WskQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| dates | string | 是 | 开始日期 |
| datee | string | 是 | 结束日期 |

**请求示例**

```json
{
    "page": 1,
    "size": 50,
    "dates": "2026-08-01",
    "datee": "2026-08-14"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "id": 44952,
                "chuanghao": "1",
                "huohao": "test001",
                "color": "黑色",
                "chima": "S",
                "worktype": "结腰布",
                "sl": 20,
                "baohao": "1"
            }
        ],
        "total": 1,
        "footer": {
            "bs_total": 1,
            "sl_total": 20
        }
    },
    "timestamp": 1787536511
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 未扫描记录列表 |
| result.list.id | integer | 物料编号 |
| result.list.chuanghao | string | 床号 |
| result.list.huohao | string | 货号 |
| result.list.color | string | 颜色 |
| result.list.chima | string | 尺码 |
| result.list.worktype | string | 工序 |
| result.list.sl | integer | 预发数量 |
| result.list.baohao | string | 包号 |
| result.total | integer | 数据总数量 |
| result.footer | object | 合计 |
| result.footer.bs_total | integer | 包数 |
| result.footer.sl_total | integer | 预发数量总数 |

## 10. 手工账

### 10.1 手工账接口

**接口地址**：`/api/NetYf/PinFeng/GridPageList`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| dates | string | 是 | 开始日期 |
| datee | string | 是 | 结束日期 |

**请求示例**

```json
{
    "page": 1,
    "size": 50,
    "dates": "2026-07-01",
    "datee": "2026-08-14"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "dh": "20260814-001",
                "zhdate": "2026-08-14",
                "state": 1,
                "zhuser": "超级管理员",
                "zhuser_sh": "超级管理员",
                "id": 1,
                "dept": "001",
                "deptname": "缝制车间",
                "uid": "001",
                "uname": "测试",
                "huohao": "00001",
                "huohaoname": "911",
                "ddh": "260812-001",
                "worktype": "0001",
                "wtname": "裁剪",
                "dw": "件",
                "js": 4,
                "sl": 51,
                "cp": 1,
                "chuanghao": "154",
                "color": "红色",
                "chima": "S",
                "price": 1.43,
                "je": 72.93,
                "remark": null
            }
        ],
        "total": 1
    },
    "timestamp": 1786954473
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 手工账单据列表 |
| result.list.dh | string | 单号 |
| result.list.zhdate | string | 制单日期 |
| result.list.state | integer | 单据状态 |
| result.list.zhuser | string | 制单人 |
| result.list.zhuser_sh | string | 审核人 |
| result.list.id | integer | ID |
| result.list.dept | string | 部门编号 |
| result.list.deptname | string | 部门 |
| result.list.uid | string | 员工工号 |
| result.list.uname | string | 员工 |
| result.list.huohao | string | 货号编号 |
| result.list.huohaoname | string | 货号名称 |
| result.list.ddh | string | 计划单号 |
| result.list.worktype | string | 工序编号 |
| result.list.wtname | string | 工序名称 |
| result.list.dw | string | 单位 |
| result.list.js | integer | 件数 |
| result.list.sl | integer | 预发数量 |
| result.list.cp | integer | 次品 |
| result.list.chuanghao | string | 床号 |
| result.list.color | string | 颜色 |
| result.list.chima | string | 尺码 |
| result.list.price | number | 单价 |
| result.list.je | number | 金额 |
| result.list.remark | string | 备注 |
| result.total | integer | 数据总数量 |

## 11. 吊挂对接

### 11.1 吊挂对接中间库接口

**接口地址**：`/api/NetYf/Dg/GridPageList`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |

**请求示例**

```json
{
    "page": 1,
    "size": 50
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "id": 1,
                "dg_type": 4,
                "dg_name": null,
                "dg_Server": "1",
                "dg_Database": "1",
                "dg_Uid": "1",
                "dg_Pwd": "1"
            }
        ],
        "total": 1
    },
    "timestamp": 1786959685
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 吊挂中间库配置列表 |
| result.list.id | integer | ID |
| result.list.dg_type | integer | 吊挂公司 |
| result.list.dg_name | string | 吊挂名称 |
| result.list.dg_Server | string | 吊挂中间库地址 |
| result.list.dg_Database | string | 吊挂中间库数据库名称 |
| result.list.dg_Uid | string | 数据库登录账号 |
| result.list.dg_Pwd | string | 数据库登录密码 |
| result.total | integer | 数据总数量 |

### 11.2 吊挂组别接口

**接口地址**：`/api/NetYf/Dg/DgZuGridPageList`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |

**请求示例**

```json
{
    "page": 1,
    "size": 50
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "id": 1,
                "dgname": "connstr1",
                "xianhao": "1",
                "zuBieName": "1组"
            }
        ],
        "total": 1
    },
    "timestamp": 1786715639
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 吊挂组别列表 |
| result.list.id | integer | ID |
| result.list.dgname | string | 吊挂名称 |
| result.list.xianhao | string | 线号 |
| result.list.zuBieName | string | 组别名称 |
| result.total | integer | 数据总数量 |

### 11.3 吊挂工序产能

**接口地址**：`/api/NetYf/Dg/DgClQuery`

**业务请求参数**

| 参数名 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| page | integer | 是 | 当前页 |
| size | integer | 是 | 每页大小 |
| dates | string | 是 | 开始日期 |
| datee | string | 是 | 结束日期 |

**请求示例**

```json
{
    "page": 1,
    "size": 50,
    "dates": "2026-08-01",
    "datee": "2026-08-14"
}
```

**响应示例**

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "list": [
            {
                "id": 1249729,
                "rq": "2026-08-01",
                "dddh": "26YR8-6",
                "chuanghao": "4",
                "huohao": "00303",
                "bbreed": "MC26800MI-1",
                "color": "黑色/白色波点",
                "chima": "M",
                "worktype": "0280",
                "wtname": "上V腰",
                "uid": "03021",
                "uname": "03021",
                "dguid": "03021",
                "dguname": "唐敏",
                "dept": "006",
                "dgName": "connstr1",
                "dgStyleNo": "5",
                "sl": 146,
                "price": 0.35,
                "je": 51.1,
                "sfjz": 0
            }
        ],
        "total": 1
    },
    "timestamp": 1786959725
}
```

**响应字段说明**

| 参数名 | 类型 | 说明 |
| --- | --- | --- |
| result.list | array | 吊挂产量明细列表 |
| result.list.id | integer | ID |
| result.list.rq | string | 生产时间 |
| result.list.dddh | string | 订单号 |
| result.list.chuanghao | string | 床号 |
| result.list.huohao | string | 货号编号 |
| result.list.bbreed | string | 货号名称 |
| result.list.color | string | 颜色 |
| result.list.chima | string | 尺码 |
| result.list.worktype | string | 工序编号 |
| result.list.wtname | string | 工序名称 |
| result.list.uid | string | 员工工号 |
| result.list.uname | string | 员工名称 |
| result.list.dguid | string | 吊挂工号 |
| result.list.dguname | string | 吊挂员工名称 |
| result.list.dept | string | 人事部门 |
| result.list.dgName | string | 吊挂线 |
| result.list.dgStyleNo | string | 线号 |
| result.list.sl | integer | 产量 |
| result.list.price | number | 单价 |
| result.list.je | number | 金额 |
| result.list.sfjz | integer | 是否结账 |
| result.total | integer | 数据总数量 |
