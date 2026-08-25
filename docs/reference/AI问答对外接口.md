# 全局公共参数

**全局Header参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**全局Query参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**全局Body参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**全局认证方式**

> 无需认证

# 状态码说明

| 状态码 | 中文描述 |
| --- | ---- |
| 暂无参数 |

# API说明

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:57:20

> 更新时间: 2026-08-25 09:06:06

## API接口接入说明

### 基础请求信息

**请求根地址：http://hzlinkbiz.ywhzsoft.com:9002
默认服务端口：9002**

```
http://hzlinkbiz.ywhzsoft.com:9002
```

#### Token获取接口示例

**完整请求地址：http://hzlinkbiz.ywhzsoft.com:9002/api/system/token
请求方式：POST**

```
http://hzlinkbiz.ywhzsoft.com:9002/api/system/token
```

##### 请求体参数(测试可以用这个调试)

```json
{
  "app_key": "BB/GTvh1GXQ5o9SXC8uvHadfRNoIekgAcItBtvowNePdpbSVsDFNEoxXUF/qU+hPnmr8itELafkhnwx41B0gfzxeqxKDYGe92pyqplxbQOyJ6pg+MqDNxHUYOgSUImEZ"   //用户访问智能体的时候携带,根据这个拿到token和sign,timestamp
}
```

##### 成功返回示例

```json
{
    "code": 1,
    "message": "成功",
    "result": {
        "tokenType": "Bearer",
        "accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiMDEwMDEiLCJ1bmFtZSI6IuadqOWfuuWxsSIsImxvZ2luVXNlck5hbWUiOiIiLCJsb2dpblJlYWxOYW1lIjpudWxsLCJjdXN0b21JZCI6IjczODhDNDZFLUI0RkUtNDZEMy1CRjZDLTc1RThDQkY3NEU3QyIsInVzZXJUeXBlIjoi5bCP56iL5bqP55So5oi3IiwiaWF0IjoxNzg3NjE4NjgwLCJuYmYiOjE3ODc2MTg2ODAsImV4cCI6MTc4NzYyNTg4MCwiaXNzIjoiSHpEdWlKaWVTZXJ2ZXIiLCJhdWQiOiJIekR1aUppZVNlcnZlci5BcGlDbGllbnRzIn0.xiqWg-U8ixwBp5bWJMPR4JrFJK1eg25z5LoesVP7dRY",
        "expiresIn": 7200,
        "expiresAt": "2026-08-25T02:44:40.7744103+00:00",
        "user": "01001",
        "uname": "杨基山",
        "loginUserName": "",
        "appkey": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
        "sign": "6eff249601e51ee6d33cf7a8c333b156",
        "timestamp": 1787618680,
        "roles": [],
        "permissions": []
    },
    "timestamp": 1787618680
}
```

### 对接配置信息

**弘兆唯一对接密钥（AppSecret）：4fU5aP8xR2tY7kLlzQ9sW5eH7cB3nM6vD8
时间戳有效期：默认60秒**

```
4fU5aP8xR2tY7kLlzQ9sW5eH7cB3nM6vD8
```

### 签名生成规则


 1. 将请求参数中除sign外的所有键值对，按键名的字典序升序排序，按照key1value1key2value2...的格式拼接为字符串，示例拼接结果：app_key=5799A25A-BBFE-4C97-8556-64C52B2203A2timestamp=1787451063
 2. 将AppSecret拼接在上述排序后的字符串头部，得到最终待加密签名字符串：4fU5aP8xR2tY7kLlzQ9sW5eH7cB3nM6vD8app_key=5799A25A-BBFE-4C97-8556-64C52B2203A2timestamp=1787451063
 3. 使用md5算法对该签名字符串进行加密，转换为32位小写十六进制字符串即为sign参数值。
示例签名结果：4b10d3e4f717c79bf1f33af853914fcb

**使用md5算法对该签名字符串进行加密，转换为32位小写十六进制字符串即为sign参数值。
示例签名结果：4b10d3e4f717c79bf1f33af853914fcb**

```
undefined
```

### 接口请求参数说明

| 参数名 | 类型  | 必填  | 说明  |
| --- | --- | --- | --- |
| app_key | string | 是 | 弘兆分配给应用的app_key |
| timestamp | long | 是 | 秒级时间戳 |
| sign | string | 是 | 接口请求签名 |

```
undefined
```

### 错误码说明

| 错误码 | 错误信息 | 排查方法 |
| --- | ---- | ---- |
| 0 | app_key不能为空 | 检查app_key是否为空值 |
| 0 | 请求已过期 | 请求超过60秒自动过期 |
| 0 | 签名无效 | 检查数字签名是否正确 |
| 0 | 无效app_key | 检查app_key是否输入正确 |
| 0 | 加密信息解析失败,请检查参数是否正确 | 用户访问智能体的时候携带的数据有误 |

```
undefined
```

**Query**

# 接口清单

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:19:23

> 更新时间: 2026-08-23 10:48:40

#### 身份认证与权限类


 - 获取签名
 - 获取访问令牌
 - 测试权限

#### 货号管理类


 - 货号信息单据接口
 - 货号信息表单接口
 - 货号工序接口

#### 生产管理类


 - 生产计划接口
 - 生产类型接口
 - 生产制单接口
 - 生产制单工序接口
 - 生产制单扫描接口
 - 生产工序接口
 - 手工账接口

#### 吊挂系统对接类


 - 吊挂对接中间库接口
 - 吊挂组别接口
 - 吊挂工序产能

#### 工序与产能类


 - 线下工序产能
 - 工序产量查询接口
 - 工序进度查询接口

#### 员工与薪资类


 - 员工信息接口
 - 工资明细/汇总查询接口
 - 员工工资排名查询接口

#### 生产查询类


 - 生产查询-已扫描接口
 - 生产查询-未扫描接口

**Query**

# AI问答对外相关接口

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-23 12:58:23

```text
暂无描述
```

**目录Header参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Query参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Body参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | F6B63C9C-2E08-42FF-A926-28C49C78822B | string | 是 | 弘兆分配给应用的app_key, |

**目录认证信息**

> 继承父级

**Query**

## 获取访问令牌(Token)

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-25 09:05:32

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://hzlinkbiz.ywhzsoft.com:9002/api/system/token

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "BB/GTvh1GXQ5o9SXC8uvHadfRNoIekgAcItBtvowNePdpbSVsDFNEoxXUF/qU+hPnmr8itELafkhnwx41B0gfzxeqxKDYGe92pyqplxbQOyJ6pg+MqDNxHUYOgSUImEZ"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5CLpAKhdvc2HWeCf2bSnwGYhk9PPx6IdbN9lz1PgGSXjDzufzuNjJuKndJqzGjvtnmr8itELafkhnwx41B0gfzxeqxKDYGe92pyqplxbQOyJ6pg+MqDNxHUYOgSUImEZ | string | 是 | 用户访问智能体的时候携带,根据这个拿到token和sign |

**认证方式**

> 继承父级

**响应示例**

* 成功(200)

```javascript
{
	"code": 1,
	"message": "成功",
	"result": {
		"tokenType": "Bearer",
		"accessToken": "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiMDEwMDEiLCJ1bmFtZSI6IuadqOWfuuWxsSIsImxvZ2luVXNlck5hbWUiOiIiLCJsb2dpblJlYWxOYW1lIjpudWxsLCJjdXN0b21JZCI6IjczODhDNDZFLUI0RkUtNDZEMy1CRjZDLTc1RThDQkY3NEU3QyIsInVzZXJUeXBlIjoi5bCP56iL5bqP55So5oi3IiwiaWF0IjoxNzg3NjE4NjgwLCJuYmYiOjE3ODc2MTg2ODAsImV4cCI6MTc4NzYyNTg4MCwiaXNzIjoiSHpEdWlKaWVTZXJ2ZXIiLCJhdWQiOiJIekR1aUppZVNlcnZlci5BcGlDbGllbnRzIn0.xiqWg-U8ixwBp5bWJMPR4JrFJK1eg25z5LoesVP7dRY",
		"expiresIn": 7200,
		"expiresAt": "2026-08-25T02:44:40.7744103+00:00",
		"user": "01001",
		"uname": "杨基山",
		"loginUserName": "",
		"appkey": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
		"sign": "6eff249601e51ee6d33cf7a8c333b156",
		"timestamp": 1787618680,
		"roles": [],
		"permissions": []
	},
	"timestamp": 1787618680
}
```

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | - |
| result.tokenType | Bearer | string | - |
| result.accessToken | eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJ1c2VyIjoiMDEwMDEiLCJ1bmFtZSI6IuadqOWfuuWxsSIsImxvZ2luVXNlck5hbWUiOiIiLCJsb2dpblJlYWxOYW1lIjpudWxsLCJjdXN0b21JZCI6IjU3OTlBMjVBLUJCRkUtNEM5Ny04NTU2LTY0QzUyQjIyMDNBMiIsInVzZXJUeXBlIjoi5bCP56iL5bqP55So5oi3IiwiaWF0IjoxNzg3NTc2Mjk1LCJuYmYiOjE3ODc1NzYyOTUsImV4cCI6MTc4NzU4MzQ5NSwiaXNzIjoiSHpEdWlKaWVTZXJ2ZXIiLCJhdWQiOiJIekR1aUppZVNlcnZlci5BcGlDbGllbnRzIn0.9UTmMEwAXXDLeBhXhkQk6hsOawCnrfGa99LPFh2l3J0 | string | token |
| result.expiresIn | 7200 | string | - |
| result.expiresAt | 2026-08-24T14:58:15.6584812+00:00 | string | - |
| result.user | 01001 | string | 用户/员工 |
| result.uname | 杨基山 | string | 员工姓名 |
| result.loginUserName | - | string | ERP用户名 |
| result.appkey | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 弘兆配置的app_key |
| result.sign | 6040e353def857a9dc22db2f9e24a36c | string | 签名 |
| result.timestamp | 1787576295 | string | 签名生成的时间戳 |
| result.roles | - | string | - |
| result.permissions | - | string | - |
| timestamp | 1787576295 | string | 接口返回的时间戳 |

* 无登录权限(404)

```javascript
{
	"code": 0,
	"message": "无登录权限",
	"result": null,
	"timestamp": 1787445165
}
```

* 参数异常(400)

```javascript
{
	"code": 0,
	"message": "加密信息解析失败,请检查参数是否正确",
	"result": null,
	"timestamp": 1787578692
}
```

**Query**

## 测试权限

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-23 09:01:39

```text
暂无描述
```

**接口状态**

> 已完成

**接口URL**

> http://localhost:53785/api/print/test-permissions

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "F6B63C9C-2E08-42FF-A926-28C49C78822B",
  "timestamp": 1786367116,
  "sign": "e55dcfa1fe65d31096a135b093e5921b"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | F6B63C9C-2E08-42FF-A926-28C49C78822B | string | 是 | 弘兆分配给用户的app_key |
| timestamp | 1786360191 | integer | 是 | 时间戳 |
| sign | e55dcfa1fe65d31096a135b093e5921b | string | 是 | 数字签名,MD5加密后转小写 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
{
	"code": 1,
	"message": "成功",
	"result": "调用成功",
	"timestamp": 1786360218
}
```

* 无效app_key(400)

```javascript
{
	"code": 0,
	"message": "无效app_key",
	"result": null,
	"timestamp": 1786357650
}
```

* 请求已过期(400)

```javascript
{
	"code": 0,
	"message": "请求已过期",
	"result": null,
	"timestamp": 1786357650
}
```

* 签名无效(400)

```javascript
{
	"code": 0,
	"message": "签名无效",
	"result": null,
	"timestamp": 1786359500
}
```

**Query**

## 获取签名

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 19:42:55

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://localhost:53785/api/print/query-sign

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1787394965
}
```

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
{
	"code": 1,
	"message": "成功",
	"result": "e9fe4b3fe1c3bf26fc47116942dd2abb",
	"timestamp": 1786360106
}
```

* 无效app_key(400)

```javascript
{
	"code": 0,
	"message": "无效app_key",
	"result": null,
	"timestamp": 1786357650
}
```

* 请求已过期(400)

```javascript
{
	"code": 0,
	"message": "请求已过期",
	"result": null,
	"timestamp": 1786357650
}
```

**Query**

## 基础数据管理

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-23 09:01:39

```text
暂无描述
```

**目录Header参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Query参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Body参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录认证信息**

> 继承父级

**Query**

### 用户信息查询接口

> 创建人: 狄拉墨涅

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-24 14:12:23

> 更新时间: 2026-08-24 17:19:36

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Baseinfo/UserInfoQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "USERNAME": ""
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| USERNAME | admin | string | 是 | 登录用户 |

**认证方式**

> 继承父级

**响应示例**

* 成功(200)

```javascript
{
	"code": 1,
	"message": "成功",
	"result": {
		"list": [
			{
				"code": "Admin",
				"username": "Admin",
				"realname": "管理员",
				"companyName": "厉精H6演示"
			}
		],
		"total": 1
	},
	"timestamp": 1787554551
}
```

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.code | Admin | string | - |
| result.list.username | Admin | string | - |
| result.list.realname | 管理员 | string | - |
| result.list.companyName | 厉精H6演示 | string | - |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1787554551 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 用户菜单查询接口

> 创建人: 狄拉墨涅

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-24 17:42:14

> 更新时间: 2026-08-24 20:48:53

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://localhost:53785/api/NetYf/Baseinfo/MoveMenuQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |

**认证方式**

> 继承父级

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.uid | 004 | string | - |
| result.list.uname | 庞保忠 | string | 姓名 |
| result.list.dept | - | string | - |
| result.list.menus | - | string | - |
| result.list.menus.name | 扫码计件 | string | 菜单 |
| result.list.menus.model | 工作台 | string | 菜单 |
| result.list.menus.isScan | true | string | 进入是否调用扫描 |
| result.list.menus.sort | 10101 | string | 排序 |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1787565615 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 货号信息单据接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:24:17

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Baseinfo/HuohaoQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |

**认证方式**

> 继承父级

**响应示例**

* 成功(200)

```javascript
{
	"code": 1,
	"message": "成功",
	"result": {
		"huohaoList": [ //货号信息
			{
				"bh": "00001", //货号编码
				"bbreed": "911", //货号
				"name_pk": "911",
				"description": "女士长袖", //品名
				"stype": "0001", //货号类型编码
				"huohaotype": "衣服", //货号类型
				"dw": "件", //单位
				"lpinpai": "", //品牌
				"isdelete": 0,
				"jst_huohao": "911"
			}
		],
		"hh_total": 1,
		"huohaoTypeList": [ //货号类型数据集
			{
				"id": 1,
				"bh": "0001",
				"pbh": "#",
				"name": "衣服", //货号类型
				"name_pk": "yf",
				"isdelete": 0
			}
		],
		"ht_total": 1
	},
	"timestamp": 1786540713
}
```

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.huohaoList | - | string | 货号信息集合 |
| result.huohaoList.bh | 00001 | string | 货号编码 |
| result.huohaoList.bbreed | 911 | string | 货号 |
| result.huohaoList.name_pk | 911 | string | 货号简拼 |
| result.huohaoList.description | 女士长袖 | string | 品名 |
| result.huohaoList.stype | 0001 | string | 货号类型编码 |
| result.huohaoList.huohaotype | 衣服 | string | 货号类型 |
| result.huohaoList.dw | 件 | string | 单位 |
| result.huohaoList.lpinpai | - | string | 品牌 |
| result.huohaoList.isdelete | 0 | string | 是否删除 |
| result.huohaoList.jst_huohao | 911 | string | 聚水潭货号 |
| result.hh_total | 1 | string | 货号信息总数 |
| result.huohaoTypeList | - | string | 货号类型集合 |
| result.huohaoTypeList.id | 1 | string | ID |
| result.huohaoTypeList.bh | 0001 | string | 货号编码 |
| result.huohaoTypeList.pbh | # | string | 根目录编号 |
| result.huohaoTypeList.name | 衣服 | string | 货号类型 |
| result.huohaoTypeList.name_pk | yf | string | 货号类型简拼 |
| result.huohaoTypeList.isdelete | 0 | string | 是否删除 |
| result.ht_total | 1 | string | 货号类型总数 |
| timestamp | 1786540713 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 货号信息表单接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:25:51

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Baseinfo/HuohaoFormQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "huohao":"00001"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | F6B63C9C-2E08-42FF-A926-28C49C78822B | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| huohao | 00001 | string | 是 | 货号编号 |

**认证方式**

> 继承父级

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.huohaoList | - | string | 货号集合 |
| result.huohaoList.bh | 00001 | string | 货号编号 |
| result.huohaoList.bbreed | 911 | string | 货号名称 |
| result.huohaoList.name_pk | 911 | string | 货号简拼 |
| result.huohaoList.description | 女士长袖 | string | 品名 |
| result.huohaoList.stype | 0001 | string | - |
| result.huohaoList.huohaotype | 衣服 | string | - |
| result.huohaoList.dw | 件 | string | 单位 |
| result.huohaoList.lpinpai | - | string | 品牌编号 |
| result.huohaoList.isdelete | 0 | string | 是否删除 |
| result.huohaoList.jst_huohao | 911 | string | 聚水潭货号 |
| result.hh_total | 1 | string | 货号信息总数 |
| result.huohaoColorList | - | string | 货号颜色集合 |
| result.huohaoColorList.id | 1 | string | ID |
| result.huohaoColorList.bh | 00001 | string | 货号编号 |
| result.huohaoColorList.color | 红色 | string | 颜色 |
| result.huohaoColorList.uploadguid | 98A6CC9A-C3A9-8776-B232-D7FD027ECD6E | string | 图片guid |
| result.hc_total | 1 | string | 货号颜色总数 |
| result.huohaoChimaList | - | string | 货号尺码集合 |
| result.huohaoChimaList.id | 1 | string | ID |
| result.huohaoChimaList.bh | 00001 | string | 货号编号 |
| result.huohaoChimaList.chima | S | string | 尺码 |
| result.huohaoChimaList.banx | - | string | 版型 |
| result.huohaoChimaList.kez | - | string | 克重 |
| result.huohaoChimaList.xs_price | 0 | string | 销售价 |
| result.huohaoChimaList.price | 0 | string | 单价 |
| result.hs_total | 1 | string | 货号尺码总数 |
| timestamp | 1786544009 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 生产类型接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:26:11

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Baseinfo/ScTypeQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe"
}
```

**认证方式**

> 继承父级

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.sctypeList | - | string | 生产类型集合 |
| result.sctypeList.bh | 0001 | string | 生产类型编号 |
| result.sctypeList.name | 整件 | string | 生产类型名称 |
| result.sctypeList.name_pk | zj | string | 简拼 |
| result.sctypeList.sfjcj | 1 | string | 是否计裁剪 |
| result.sctypeList.isdelete | 0 | string | 是否删除 |
| result.sctypeList.sfcprk | 0 | string | 是否成品入库 |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1786696855 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 生产工序接口

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 21:02:25

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://localhost:53785/api/NetYf/Baseinfo/RfidWorktypeQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1787576468,
  "sign": "9ec72a3e3c634ef71f0a4a9d77758640"
}
```

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.worktypeList | - | string | 生产工序集合 |
| result.worktypeList.bh | 0001 | string | 编号 |
| result.worktypeList.name | 裁剪 | string | 工序 |
| result.worktypeList.name_pk | cj | string | 简码 |
| result.worktypeList.gxtype | 0 | string | 流转类型(0:工序流转,1:仓库流转,2:验布工资专用) |
| result.worktypeList.isdelete | 0 | string | 是否删除 |
| result.worktypeList.section | 前道 | string | 工段 |
| result.worktypeList.jc | - | string | 简称 |
| result.worktypeList.sc_type | - | string | 系统工序 |
| result.worktypeList.worktype_group | - | string | 工序组 |
| result.worktypeList.yfgs | 0 | string | 预发改数 |
| result.worktypeList.default_price | - | string | 默认工价 |
| result.worktypeList.gongzi_js_type | 1 | string | 工资结算方式(0:预发,1;实收,2:多次实收) |
| result.worktypeList.wt_sort | - | string | 工序号 |
| result.worktypeList.xz_price | - | string | 限制工价 |
| result.worktypeList.default_working_hours | 42 | string | 默认理论工时 |
| result.worktypeList.vehicle_type | 平车 | string | 车种 |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1786696978 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 货号工序接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:27:00

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Baseinfo/HuohaoWorktypeQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "huohao": "00001"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| huohao | 00001 | string | 是 | 货号编号 |

**认证方式**

> 继承父级

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.id | 3 | string | ID |
| result.list.huohao | 00001 | string | 货号 |
| result.list.huohaoname | 25-MMT40218F第8-1单 | string | 货号 |
| result.list.wt | 0001 | string | 工序编号 |
| result.list.wtname | 验布 | string | 工序名称 |
| result.list.sort | 1 | string | 序号 |
| result.list.sctype | 0002 | string | 生产类型编号 |
| result.list.sctypename | 大身 | string | 生产类型 |
| result.list.sfzb | 0 | string | 是否整版(0:否,1:是) |
| result.list.using_state | 1 | string | 使用中(0:否,1:是) |
| result.list.zhgx | 0 | string | 最后工序(0:否,1:是) |
| result.list.sfxs | 0 | string | 是否线上(0:否,1:是) |
| result.list.theoretical_work_hours | 42 | string | 理论工时 |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1786950207 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 员工信息接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:27:20

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Baseinfo/EmployeeQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "uid": "01001"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| uid | 001 | string | 否 | 员工工号 |

**认证方式**

> 继承父级

**响应示例**

* 成功(200)

```javascript
{
	"code": 1,
	"message": "成功",
	"result": {
		"employeeList": [
			{
				"uid": "001",
				"uname": "测试",
				"name_pk": "cs",
				"mobile": "13574587458",
				"movepassword": "123",
				"move_Login": 1,
				"dept": "001",
				"deptname": "缝制车间",
				"employeeRule": "[\"0001\"]",
				"move_scan": 1,
				"loginUserName": "",
				"zr_ck": "",
				"dy_gongzhong": "",
				"move_admin_role": "02"
			}
		],
		"total": 1
	},
	"timestamp": 1786697009
}
```

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.employeeList | - | string | 员工集合 |
| result.employeeList.uid | 001 | string | 员工工号 |
| result.employeeList.uname | 测试 | string | 员工姓名 |
| result.employeeList.name_pk | cs | string | 简码 |
| result.employeeList.mobile | 13574587458 | string | 手机号 |
| result.employeeList.movepassword | 123 | string | 移动登录密码 |
| result.employeeList.move_Login | 1 | string | 移动登录权限 |
| result.employeeList.dept | 001 | string | 所属部门 |
| result.employeeList.deptname | 缝制车间 | string | 所属部门名称 |
| result.employeeList.employeeRule | ["0001"] | string | 分配角色 |
| result.employeeList.move_scan | 1 | string | 移动扫描方式(0:绑定工序,1:选择工序) |
| result.employeeList.loginUserName | - | string | 登录用户绑定 |
| result.employeeList.zr_ck | - | string | 仓库绑定 |
| result.employeeList.dy_gongzhong | - | string | 打样工种 |
| result.employeeList.move_admin_role | 02 | string | 移动管理员(01:是,02:否) |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1786697009 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 部门信息接口

> 创建人: 狄拉墨涅

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-24 21:37:57

> 更新时间: 2026-08-24 21:55:24

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Baseinfo/DeptQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1787576468,
  "sign": "9ec72a3e3c634ef71f0a4a9d77758640"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1787576468 | string | 是 | - |
| sign | 9ec72a3e3c634ef71f0a4a9d77758640 | string | 是 | - |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.deptList | - | string | - |
| result.deptList.id | 001 | string | 部门 |
| result.deptList.name | 研发部 | string | 部门名称 |
| result.deptList.remark | - | string | 备注 |
| result.deptList.name_pk | yfb | string | 简拼 |
| result.deptList.isdelete | 0 | string | 是否删除 |
| result.deptList.sysdept | 本厂 | string | 系统部门 |
| result.deptList.company | 0001 | string | 公司编码 |
| result.deptList.companyName | 宇鹏 | string | 公司名称 |
| result.deptList.pid | 01 | string | - |
| result.total | 2 | string | 数据总数量 |
| timestamp | 1786696978 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

## 生产计划

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-23 09:01:39

```text
暂无描述
```

**目录Header参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Query参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Body参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录认证信息**

> 继承父级

**Query**

### 生产计划接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:02:32

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://localhost:53785/api/NetYf/Plan/GridPageList

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "page": 1,
  "size": 50,
  "dates": "2026-08-01",
  "datee": "2026-08-14"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| page | 1 | string | 是 | 当前页 |
| size | 50 | string | 是 | 每页大小 |
| dates | 2026-08-01 | string | 是 | 开始日期 |
| datee | 2026-08-14 | string | 是 | 结束日期 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.dh | jh20260810-004 | string | 单号 |
| result.list.zhdate | 2026-08-10 | string | 下单日期 |
| result.list.finish_date | 2026-08-15 | string | 交货日期 |
| result.list.jhdh | 26xiyin8-7 | string | 计划单号 |
| result.list.hth | - | string | 合同号 |
| result.list.gdy | 顾颖 | string | 跟单员 |
| result.list.zdr | 顾颖 | string | 制单人 |
| result.list.zsl | 100 | string | 总数量 |
| result.list.zdr_sh | 代田田 | string | 审核人 |
| result.list.state | 1 | string | 单据状态(1:审核) |
| result.list.id | 94CD0DAE-141D-4384-BB9E-6EF51A2FBD99 | string | ID |
| result.list.khddh | 26xiyin8-7 | string | 客户订单号 |
| result.list.pinpai | - | string | 品牌编号 |
| result.list.pinpainame | - | string | 品牌 |
| result.list.khid | 00162 | string | 客户编号 |
| result.list.khname | SHEIN | string | 客户 |
| result.list.khhh | - | string | 客户货号 |
| result.list.huohao | 00374 | string | 货号 |
| result.list.huohaoname | XY008 | string | 货号 |
| result.list.spname | 长裤 | string | 品名 |
| result.list.color | 黑色印花 | string | 颜色 |
| result.list.chima | S | string | 尺码 |
| result.list.dw | 条 | string | 单位 |
| result.list.ddsl | 20 | string | 订单 |
| result.list.paol | - | string | 抛量 |
| result.list.sl | 20 | string | 预发数量 |
| result.list.remark | - | string | 备注 |
| result.total | 1 | string | 总数据数量 |
| timestamp | 1786949808 | string | - |

**Query**

## 生产制单

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-23 09:01:39

```text
暂无描述
```

**目录Header参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Query参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Body参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录认证信息**

> 继承父级

**Query**

### 生产制单接口

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-25 08:55:01

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Sclzd/GridPageList

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "page": 1,
  "size": 50,
  "dates": "2026-08-01",
  "datee": "2026-08-14"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| page | 1 | string | 是 | 当前页 |
| size | 50 | string | 是 | 每页大小 |
| dates | 2026-08-01 | string | 是 | 开始日期 |
| datee | 2026-08-14 | string | 是 | 结束日期 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.dh | 202608120003 | string | 单号 |
| result.list.zhdate | 2026-08-12 | string | 制单日期 |
| result.list.dddh | 260812-001 | string | 计划单号 |
| result.list.khid | 00005 | string | 客户编号 |
| result.list.khname | KY服饰演示 | string | 客户 |
| result.list.drdg_status | 0 | string | 导入吊挂(0:否,1:是) |
| result.list.huohao | 00001 | string | 货号 |
| result.list.huohaoname | 911 | string | 货号 |
| result.list.description | 女士长袖 | string | 品名 |
| result.list.sctype | 0001 | string | 生产类型编号 |
| result.list.sctypename | 整件 | string | 生产类型 |
| result.list.chuanghao | 3 | string | 床号 |
| result.list.cjr | - | string | 裁剪人 |
| result.list.zdr | 管理员 | string | 制单人 |
| result.list.state | 1 | string | 状态 |
| result.list.id | 9 | string | 物料编号 |
| result.list.baohao | 1 | string | 包号 |
| result.list.ganghao | - | string | 缸号 |
| result.list.color | 红色 | string | 颜色 |
| result.list.chima | S | string | 尺码 |
| result.list.fhsl | 20 | string | 预发数量 |
| result.list.sssl | 18 | string | 产量 |
| result.list.remark | - | string | 备注 |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1786949767 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 生产制单工序接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:10:59

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Sclzd/SclzdWorktypeQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "dh": "202608010001"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| dh | 202608010001 | string | 是 | 单号 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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
			},
			{
				"id": 58450,
				"dh": "202608010001",
				"huohao": "00092",
				"huohaoname": "MC24395MI",
				"wt": "0003",
				"wtname": "裁剪",
				"sort": 3,
				"zhgx": 0,
				"sfzb": 0,
				"sctype": "0001",
				"sctypename": "整件"
			},
			{
				"id": 58451,
				"dh": "202608010001",
				"huohao": "00092",
				"huohaoname": "MC24395MI",
				"wt": "0035",
				"wtname": "分包",
				"sort": 4,
				"zhgx": 0,
				"sfzb": 0,
				"sctype": "0001",
				"sctypename": "整件"
			},
			{
				"id": 58452,
				"dh": "202608010001",
				"huohao": "00092",
				"huohaoname": "MC24395MI",
				"wt": "0077",
				"wtname": "烫标",
				"sort": 5,
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.id | 58448 | string | ID |
| result.list.dh | 202608010001 | string | 单号 |
| result.list.huohao | 00092 | string | 货号 |
| result.list.huohaoname | MC24395MI | string | 货号名称 |
| result.list.wt | 0001 | string | 工序编号 |
| result.list.wtname | 验布 | string | 工序名称 |
| result.list.sort | 1 | string | 序号 |
| result.list.zhgx | 0 | string | 最后工序(0:否,1:是) |
| result.list.sfzb | 0 | string | 是否整版(0:否,1:是) |
| result.list.sctype | 0001 | string | 生产类型编号 |
| result.list.sctypename | 整件 | string | 生产类型 |
| result.total | 5 | string | 数据总数量 |
| timestamp | 1786950485 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 生产制单查询工序扫描接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 21:56:50

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Sclzd/SclzdBarcodeQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "dh": "202608160017",
  "detailId": 95715
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| dh | 202608160017 | string | 是 | 单号 |
| detailId | 95715 | string | 是 | 物料编号 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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
			},
			{
				"uid": "15051",
				"uname": "康子飞",
				"dept": "003",
				"worktype": "0035",
				"wtname": "分包",
				"inputtime": "2026-08-17 08:14:25"
			},
			{
				"uid": "14002",
				"uname": "张海银",
				"dept": "003",
				"worktype": "0077",
				"wtname": "烫标",
				"inputtime": "2026-08-17 08:23:21"
			}
		],
		"total": 4
	},
	"timestamp": 1786953699
}
```

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.barcodeZb | - | string | 整版信息 |
| result.barcodeZb.uid | 15046 | string | 工号 |
| result.barcodeZb.uname | 隋鑫博 | string | 员工 |
| result.barcodeZb.dept | 003 | string | 部门编码 |
| result.barcodeZb.worktype | 0003 | string | 工序编号 |
| result.barcodeZb.wtname | 裁剪 | string | 工序名称 |
| result.barcodeZb.inputtime | 2026-08-17 16:01:30 | string | 扫描时间 |
| result.totalZb | 1 | string | 整版信息总数 |
| result.barcode | - | string | 非整版信息 |
| result.barcode.uid | 15027 | string | 工号 |
| result.barcode.uname | 孙小件 | string | 员工 |
| result.barcode.dept | 003 | string | - |
| result.barcode.worktype | 0001 | string | 工序编号 |
| result.barcode.wtname | 验布 | string | 工序名称 |
| result.barcode.inputtime | 2026-08-16 23:07:51 | string | 扫描时间 |
| result.total | 4 | string | 数据总数量 |
| timestamp | 1786953699 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 线下工序产量产能

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:15:32

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Sclzd/BarcodeClQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1787445300,
  "sign": "2145f90ab64d4d293b78625f63cad9b9",
  "page": 1,
  "size": 50,
  "dates": "2026-07-01",
  "datee": "2026-08-14"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| page | 1 | string | 是 | 当前页 |
| size | 50 | string | 是 | 每页大小 |
| dates | 2026-07-01 | string | 是 | 开始日期 |
| datee | 2026-08-14 | string | 是 | 结束日期 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.inputtime | 2026-08-12T22:07:56.17 | string | 刷卡时间 |
| result.list.uid | 88888 | string | 员工工号 |
| result.list.uname | 演示员工 | string | 员工姓名 |
| result.list.dept | 001 | string | 人事部门 |
| result.list.deptname | 缝制车间 | string | 人事部门名称 |
| result.list.rq | 2026-08-12 | string | 制单日期 |
| result.list.chuanghao | 2 | string | 床号 |
| result.list.sctype | 0001 | string | 生产类型 |
| result.list.sctypename | 整件 | string | 生产类型名称 |
| result.list.baohao | 1 | string | 包号 |
| result.list.id | 5 | string | 物料编号 |
| result.list.huohao | 00001 | string | 货号 |
| result.list.bbreed | 911 | string | 货号名称 |
| result.list.description | 女士长袖 | string | 品名 |
| result.list.color | 红色 | string | 颜色 |
| result.list.chima | S | string | 尺码 |
| result.list.worktype | 0001 | string | 工序 |
| result.list.wtname | 裁剪 | string | 工序名称 |
| result.list.fhsl | 50 | string | 预发数量 |
| result.list.sssl | 50 | string | 产量 |
| result.list.sl | 50 | string | 预发数量 |
| result.list.price | 1 | string | 工价 |
| result.list.je | 50 | string | 金额 |
| result.total | 1 | string | 数据总数 |
| timestamp | 1786773213 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 工序产量查询接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:12:23

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Sclzd/HuohaoWtCLQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "page": 1,
  "size": 50,
  "queryFooter": true,
  "dates": "2026-08-01",
  "datee": "2026-08-14",
  "scheme": "货号工序"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C | string | 是 | - |
| timestamp | 1786371259 | integer | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| page | 1 | integer | 是 | 当前页 |
| size | 50 | integer | 是 | 每页大小 |
| queryFooter | true | boolean | 是 | 合计 |
| dates | 2026-08-01 | string | 是 | 开始日期 |
| datee | 2026-08-14 | string | 是 | 结束日期 |
| scheme | 货号工序/工序 | string | 是 | 按货号工序/按工序汇总查询 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.huohao | 724-001 | string | 货号 |
| result.list.sssl | 1000 | string | 产量 |
| result.list.worktype | 包装（测试） | string | 工序 |
| result.total | 2 | string | 数据总数量 |
| result.footer | - | string | 合计 |
| result.footer.sl_total | 9511 | string | 数据总数量 |
| timestamp | 1786773213 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 工资明细/汇总查询接口(线下+吊挂+手工账)

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-25 09:19:54

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://localhost:53785/api/NetYf/Sclzd/GongziMxQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
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

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C | string | 是 | - |
| timestamp | 1786371259 | integer | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| page | 1 | integer | 是 | 当前页 |
| size | 50 | integer | 是 | 每页大小 |
| queryFooter | true | boolean | 是 | 合计 |
| dates | 2026-08-01 | string | 是 | 开始日期 |
| datee | 2026-08-22 | string | 是 | 结束日期 |
| Uid | 001 | string | 是 | 员工工号 |
| Flag | 0 | string | 是 | 0 按扫描日期 1按审核日期 |
| Type | 0,1,2 | string | 是 | 工资类型（0=扫码产量 / 1=吊挂产量 / 2=手工账产量） |
| scheme | 汇总/hz/HZ | string | 是 | 为空时按明细查询，有值时按汇总查询 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.id | 10263 | string | 物料编号 |
| result.list.type | 手工账产量 | string | 工资 |
| result.list.rq | 2026-08-21T00:00:00 | string | 制单日期 |
| result.list.inputtime | 08-21 00:00:00 | string | 刷卡时间 |
| result.list.uid | 001 | string | 工号 |
| result.list.uname | 马师傅 | string | 员工姓名 |
| result.list.dept | 001 | string | 部门编码 |
| result.list.chuanghao | - | string | 床号 |
| result.list.baohao | 0 | string | 包号 |
| result.list.huohao | 25-CY22050MI第9-1单 | string | 货号 |
| result.list.color | - | string | 颜色 |
| result.list.chima | - | string | 尺码 |
| result.list.worktype | 拉布 | string | 工序 |
| result.list.ischeck | 1 | string | 是否审核 |
| result.list.check_time | 08-21 00:00:00 | string | 审核 |
| result.list.fhsl | 333 | string | 预发数量 |
| result.list.sl | 333 | string | 预发数量 |
| result.list.price | 0.065 | string | 工价 |
| result.list.je | 21.645 | string | 金额 |
| result.list.inputtime_raw | - | string | - |
| result.list.check_time_raw | - | string | - |
| result.total | 1 | string | 数据总数量 |
| result.footer | - | string | 合计 |
| result.footer.bs_total | 22 | string | 包数 |
| result.footer.fhsl_total | 7818 | string | 预发数量 |
| result.footer.sl_total | 7813 | string | 数据总数量 |
| result.footer.je_total | 2111.885 | string | 金额 |
| timestamp | 1786773213 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 工序进度查询接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-25 09:22:01

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://localhost:53785/api/NetYf/Sclzd/WorktypeProgressQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "page": 1,
  "size": 50,
  "userid": "4496",
  "uid": ""
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| page | 1 | string | 是 | 当前页 |
| size | 50 | string | 是 | 每页大小 |
| userid | 44969 | string | 是 | 物料编号 |
| uid | - | string | 是 | 员工工号 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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
			},
			{
				"userid": 4496,
				"huohao": "25-CY22050MI第9-1单",
				"color": "深紫风暴印花",
				"chima": "S",
				"baohao": "1",
				"chuanghao": "11",
				"fhsl": 11,
				"worktype": "0038",
				"name": "拉腰皮",
				"uid": "02007",
				"uname": "张培伟",
				"dept": "005",
				"inputtime": "2025-09-16T07:41:50.077",
				"cid": 11862,
				"zpsl": 11,
				"wsort": 7
			},
			{
				"userid": 4496,
				"huohao": "25-CY22050MI第9-1单",
				"color": "深紫风暴印花",
				"chima": "S",
				"baohao": "1",
				"chuanghao": "11",
				"fhsl": 11,
				"worktype": "0039",
				"name": "压腰皮",
				"uid": "02021",
				"uname": "张甜甜",
				"dept": "005",
				"inputtime": "2025-09-16T10:55:01.763",
				"cid": 11863,
				"zpsl": 11,
				"wsort": 8
			}
		],
		"total": 4
	},
	"timestamp": 1787539860
}
```

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.userid | 4496 | string | - |
| result.list.huohao | 25-CY22050MI第9-1单 | string | 货号 |
| result.list.color | 深紫风暴印花 | string | 颜色 |
| result.list.chima | S | string | 尺码 |
| result.list.baohao | 1 | string | 包号 |
| result.list.chuanghao | 11 | string | 床号 |
| result.list.fhsl | 11 | string | 预发数量 |
| result.list.worktype | 0036 | string | 工序 |
| result.list.name | 烫里腰标 | string | 工序名称 |
| result.list.uid | 14007 | string | 工号 |
| result.list.uname | 杨再军 | string | 员工姓名 |
| result.list.dept | 008 | string | 部门编码 |
| result.list.inputtime | 2025-10-17T08:36:49.267 | string | 刷卡时间 |
| result.list.cid | 11860 | string | - |
| result.list.zpsl | 11 | string | 正品数量 |
| result.list.wsort | 5 | string | 排序 |
| result.total | 4 | string | 数据总数量 |
| timestamp | 1787539860 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 员工工资排名查询接口

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-25 08:36:42

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://localhost:53785/api/NetYf/Sclzd/GongziJeOrderQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1787587942,
  "sign": "47dd82bc483d85fed8270a5eef4d50a9",
  "page": 1,
  "size": 50,
  "queryFooter": true,
  "dates": "2026-08-01",
  "datee": "2026-08-14"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C | string | 是 | - |
| timestamp | 1786371259 | integer | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| page | 1 | integer | 是 | 当前页 |
| size | 50 | integer | 是 | 每页大小 |
| queryFooter | true | boolean | 是 | 合计 |
| dates | 2026-08-01 | string | 是 | 开始日期 |
| datee | 2026-08-14 | string | 是 | 结束日期 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.uid | 04061 | string | 工号 |
| result.list.uname | 王芝兰 | string | 员工姓名 |
| result.list.dept | 007 | string | 人事部门 |
| result.list.bs | 548 | string | 包数 |
| result.list.je | 6660.52 | string | 金额 |
| result.total | 1 | string | 数据总数量 |
| result.footer | - | string | 合计 |
| result.footer.je_total | 12100 | string | 金额 |
| timestamp | 1787537015 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 生产查询-已扫描接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-25 09:23:15

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://localhost:53785/api/NetYf/Sclzd/YskQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "page": 1,
  "size": 20,
  "dates": "2026-08-01",
  "datee": "2026-08-22",
  "Uid": "001"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| page | 1 | string | 是 | 当前页 |
| size | 50 | string | 是 | 每页大小 |
| dates | 2026-07-01 | string | 是 | 开始日期 |
| datee | 2026-08-14 | string | 是 | 结束日期 |
| Uid | 001 | string | 是 | 员工工号 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
{
	"code": 1,
	"message": "成功",
	"result": {
		"list": [
			{
				"inputtime": "2026-08-04 17:29:10", //扫描时间
				"inputtime_raw": null,
				"uname": "马师傅", //员工姓名
				"uid": "001", //员工工号
				"dept": "001", //部门编码
				"id": 44791, //物料编号
				"chuanghao": "1090", //床号
				"baohao": "3", //包号
				"huohao": "724-001", //货号
				"color": "雀灰豹纹印花", //颜色
				"chima": "L", //
				"worktype": "包装（测试）", //
				"fhsl": 1000, //发卡数量
				"sl": 1000, // 产量
				"price": 0, // 工价
				"je": 0, // 金额
				"cid": 236519, //
				"sffb": 0, // 是否分包
				"fbid": 236519 //
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.inputtime | 2026-08-04 17:29:10 | string | 刷卡时间 |
| result.list.inputtime_raw | - | string | - |
| result.list.uname | 马师傅 | string | 员工姓名 |
| result.list.uid | 001 | string | 员工工号 |
| result.list.id | 44791 | string | 物料编号 |
| result.list.chuanghao | 1090 | string | 床号 |
| result.list.baohao | 3 | string | 包号 |
| result.list.huohao | 724-001 | string | 货号 |
| result.list.color | 雀灰豹纹印花 | string | 颜色 |
| result.list.chima | L | string | 尺码 |
| result.list.worktype | 包装（测试） | string | 工序 |
| result.list.fhsl | 1000 | string | 发卡数量 |
| result.list.sl | 1000 | string | 产量 |
| result.list.price | 0 | string | 工价 |
| result.list.je | 0 | string | 金额 |
| result.list.cid | 236519 | string | - |
| result.list.sffb | 0 | string | 是否分包 |
| result.list.fbid | 236519 | string | - |
| result.total | 1 | string | 数据总数量 |
| result.footer | - | string | 合计 |
| result.footer.bs_total | 13 | string | 包数 |
| result.footer.sl_total | 7405 | string | 数据总数量 |
| result.footer.je_total | 1400 | string | 金额 |
| timestamp | 1787536722 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

### 生产查询-未扫描接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:15:27

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Sclzd/WskQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "7388C46E-B4FE-46D3-BF6C-75E8CBF74E7C",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "page": 1,
  "size": 50,
  "dates": "2026-08-01",
  "datee": "2026-08-14"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | 5799A25A-BBFE-4C97-8556-64C52B2203A2 | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| page | 1 | string | 是 | 当前页 |
| size | 50 | string | 是 | 每页大小 |
| dates | 2026-07-01 | string | 是 | 开始日期 |
| datee | 2026-08-14 | string | 是 | 结束日期 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
{
	"code": 1,
	"message": "成功",
	"result": {
		"list": [
			{
				"id": 44952, //物料编号
				"chuanghao": "1", //床号
				"huohao": "test001", //货号
				"color": "黑色", //颜色
				"chima": "S", //尺码
				"worktype": "结腰布", //工序
				"sl": 20, //预发数量
				"baohao": "1" //包号
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.id | 44952 | string | 物料编号 |
| result.list.chuanghao | 1 | string | 床号 |
| result.list.huohao | test001 | string | 货号 |
| result.list.color | 黑色 | string | 颜色 |
| result.list.chima | S | string | 尺码 |
| result.list.worktype | 结腰布 | string | 工序 |
| result.list.sl | 20 | string | 预发数量 |
| result.list.baohao | 1 | string | 包号 |
| result.total | 1 | string | 数据总数量 |
| result.footer | - | string | 合计 |
| result.footer.bs_total | 1 | string | 包数 |
| result.footer.sl_total | 20 | string | 数据总数量 |
| timestamp | 1787536511 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

## 手工账

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-23 09:01:39

```text
暂无描述
```

**目录Header参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Query参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Body参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录认证信息**

> 继承父级

**Query**

### 手工账接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:24:25

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://localhost:53785/api/NetYf/PinFeng/GridPageList

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "page": 1,
  "size": 50,
  "dates": "2026-07-01",
  "datee": "2026-08-14"
}
```

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| app_key | F6B63C9C-2E08-42FF-A926-28C49C78822B | string | 是 | - |
| timestamp | 1786371259 | string | 是 | - |
| sign | 4a30c20c075f9ff065f5b7d6e9c9cffe | string | 是 | - |
| page | 1 | string | 是 | 当前页 |
| size | 50 | string | 是 | 每页大小 |
| dates | 2026-07-01 | string | 是 | 开始日期 |
| datee | 2026-08-14 | string | 是 | 结束日期 |

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.dh | 20260814-001 | string | 单号 |
| result.list.zhdate | 2026-08-14 | string | 制单日期 |
| result.list.state | 1 | string | 单据状态 |
| result.list.zhuser | 超级管理员 | string | 制单人 |
| result.list.zhuser_sh | 超级管理员 | string | 审核人 |
| result.list.id | 1 | string | ID |
| result.list.dept | 001 | string | 部门编号 |
| result.list.deptname | 缝制车间 | string | 部门 |
| result.list.uid | 001 | string | 工号 |
| result.list.uname | 测试 | string | 员工 |
| result.list.huohao | 00001 | string | 货号 |
| result.list.huohaoname | 911 | string | 货号名称 |
| result.list.ddh | 260812-001 | string | 计划单号 |
| result.list.worktype | 0001 | string | 工序 |
| result.list.wtname | 裁剪 | string | 工序名称 |
| result.list.dw | 件 | string | 单位 |
| result.list.js | 4 | string | 件数 |
| result.list.sl | 51 | string | 预发数量 |
| result.list.cp | 1 | string | 次品 |
| result.list.chuanghao | 154 | string | 床号 |
| result.list.color | 红色 | string | 颜色 |
| result.list.chima | S | string | 尺码 |
| result.list.price | 1.43 | string | 单价 |
| result.list.je | 72.93 | string | 金额 |
| result.list.remark | - | string | 备注 |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1786954473 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

## 吊挂对接

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-23 09:01:39

```text
暂无描述
```

**目录Header参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Query参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录Body参数**

| 参数名 | 示例值 | 参数类型 | 是否必填 | 参数描述 |
| --- | --- | ---- | ---- | ---- |
| 暂无参数 |

**目录认证信息**

> 继承父级

**Query**

### 吊挂对接中间库接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:18:16

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> http://localhost:53785/api/NetYf/Dg/GridPageList

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "page": 1,
  "size": 50
}
```

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.id | 1 | string | ID |
| result.list.dg_type | 4 | string | 吊挂公司 |
| result.list.dg_name | - | string | 吊挂名称 |
| result.list.dg_Server | 1 | string | 吊挂中间库地址 |
| result.list.dg_Database | 1 | string | 吊挂中间库数据库名称 |
| result.list.dg_Uid | 1 | string | 数据库登录账号 |
| result.list.dg_Pwd | 1 | string | 数据库登录密码 |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1786959685 | string | - |

* 无效app_key(400)

```javascript
{

    "code": 0,
    "message": "无效app_key",
    "result": null,
    "timestamp": 1786357650
}
```

* 请求已过期(400)

```javascript
{
	"code": 0,
	"message": "请求已过期",
	"result": null,
	"timestamp": 1786357650
}
```

* 签名无效(400)

```javascript
{
	"code": 0,
	"message": "签名无效",
	"result": null,
	"timestamp": 1786357650
}
```

**Query**

### 吊挂组别接口

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:18:32

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Dg/DgZuGridPageList

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "page": 1,
  "size": 50
}
```

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.id | 1 | string | ID |
| result.list.dgname | connstr1 | string | 吊挂名称 |
| result.list.xianhao | 1 | string | 线号 |
| result.list.zuBieName | 1组 | string | 组别名称 |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1786715639 | string | - |

* 无效app_key(400)

```javascript
{
	"code": 0,
	"message": "无效app_key",
	"result": null,
	"timestamp": 1786357650
}
```

* 请求已过期(400)

```javascript
{
	"code": 0,
	"message": "请求已过期",
	"result": null,
	"timestamp": 1786357650
}
```

* 签名无效(400)

```javascript
{
	"code": 0,
	"message": "签名无效",
	"result": null,
	"timestamp": 1786357650
}
```

**Query**

### 吊挂工序产能

> 创建人: 波吕诺厄

> 更新人: 狄拉墨涅

> 创建时间: 2026-08-23 09:01:39

> 更新时间: 2026-08-24 17:18:49

```text
暂无描述
```

**接口状态**

> 开发中

**接口URL**

> /api/NetYf/Dg/DgClQuery

**请求方式**

> POST

**Content-Type**

> json

**请求Body参数**

```javascript
{
  "app_key": "5799A25A-BBFE-4C97-8556-64C52B2203A2",
  "timestamp": 1786371259,
  "sign": "4a30c20c075f9ff065f5b7d6e9c9cffe",
  "page": 1,
  "size": 50,
  "dates": "2026-08-01",
  "datee": "2026-08-14"
}
```

**认证方式**

> Bearer Token

> 在Header添加参数 Authorization，其值为在Bearer之后拼接空格和访问令牌

> Authorization: Bearer your_access_token

**响应示例**

* 成功(200)

```javascript
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

| 参数名 | 示例值 | 参数类型 | 参数描述 |
| --- | --- | ---- | ---- |
| code | 1 | string | - |
| message | 成功 | string | - |
| result | - | string | 结果集 |
| result.list | - | string | - |
| result.list.id | 1249729 | string | ID |
| result.list.rq | 2026-08-01 | string | 生产时间 |
| result.list.dddh | 26YR8-6 | string | 订单号 |
| result.list.chuanghao | 4 | string | 床号 |
| result.list.huohao | 00303 | string | 货号 |
| result.list.bbreed | MC26800MI-1 | string | 货号名称 |
| result.list.color | 黑色/白色波点 | string | 颜色 |
| result.list.chima | M | string | 尺码 |
| result.list.worktype | 0280 | string | 工序 |
| result.list.wtname | 上V腰 | string | 工序名称 |
| result.list.uid | 03021 | string | 工号 |
| result.list.uname | 03021 | string | 员工名称 |
| result.list.dguid | 03021 | string | 吊挂工号 |
| result.list.dguname | 唐敏 | string | 吊挂员工名称 |
| result.list.dept | 006 | string | 人事部门 |
| result.list.dgName | connstr1 | string | 吊挂线 |
| result.list.dgStyleNo | 5 | string | 线号 |
| result.list.sl | 146 | string | 预发数量 |
| result.list.price | 0.35 | string | 单价 |
| result.list.je | 51.1 | string | 金额 |
| result.list.sfjz | 0 | string | 是否结账 |
| result.total | 1 | string | 数据总数量 |
| timestamp | 1786959725 | string | - |

* 失败(404)

```javascript
暂无数据
```

**Query**

# 智能体平台与 MES 系统 API 对接确认问题集

> 创建人: 波吕诺厄

> 更新人: 波吕诺厄

> 创建时间: 2026-08-24 09:10:14

> 更新时间: 2026-08-24 14:50:58

## 智能体平台与 MES 系统 API 对接确认问题集

### 一、身份、权限与组织


 1. 是否已有 OAuth 2.0/OIDC 等统一身份认证？员工是直接使用客户账号登录智能体平台，还是需要单独注册？
 2. 当前没有 OAuth 2.0/OIDC 统一认证。
 3.
 4. 统一登录后，智能体平台是否可以保存最小用户映射，用于会话、订阅和定时推送？
 5. 可以。MES 本身暂不提供会话/订阅服务
 6.
 7. MES 能否提供稳定的用户唯一标识，以及用户基本信息、账号状态、角色、所属组织和管辖范围？
 8. 可以：
 9. 稳定标识：PIUSER.ID / PIUSER.CODE（RDIFramework 账号层）、sys_employee.uid（业务员工层）；
 10. 基本信息：UserInfoQuery 返回 CODE/USERNAME/REALNAME/CompanyName；EmployeeQuery 返回 uid/uname/dept/role 等；
 11. 角色/组织：sys_employee.move_admin_role（角色）、sys_employee.company（公司）、sys_employee.dept（部门）；
 12. 管辖范围：通过「区分公司权限」开关 + company / dept / move_admin_role 控制，接口已按这些字段过滤。
 13.
 14. 用户权限是由独立权限接口提供，还是由每个 MES 业务接口根据当前用户自动鉴权和过滤？MES API 的统一认证方式是什么？
 15. （Body）中携带 app_key、timestamp（秒级时间戳）、sign（按 ApiAuth.AppSecret 生成的签名摘要）。timestamp 默认 60 秒内有效。服务同时配置了 JWT 鉴权中间件，但当前动态 API 以 app_key+timestamp+sign 为主要鉴权方式。
 16.
 17. 请明确员工、管理者、老板及其他角色分别能够查看哪些数据。一人是否可能拥有多个角色，或同时管理多个车间、多个小组？
 18. 当前员工包含 move_admin_role（角色）、company（公司）、dept（部门）等字段。权限控制主要通过：
 19. 「区分公司权限」开关实现公司级数据隔离；
 20. 特定角色（如 move_admin_role="00"）会进一步限定只能查看本人数据；
 21. 部门/车间/小组维度通过 dept 字段过滤。
 22.
 23. MES 是否可以提供完整组织树，并明确工厂、车间、小组、员工之间的关系，以及调岗、借调和历史组织归属的处理方式？

**MES 是否有稳定的员工、组织、订单、款号、计划、工单和工序 ID？**

**请明确订单、款号、计划、工单、工序、工价、员工、产量、工资和进度之间的对应关系。
生产计划接口:/api/NetYf/Plan/GridPageList
生产制单接口:/api/NetYf/Sclzd/GridPageList
对应关系大致为：
生产计划 → 生产制单 → 制单明细；
明细上挂：货号（huohao）、工序（worktype）、床号（chuanghao）；
员工通过 uid 与产量、工资关联；
产量 = 实际扫描/录入数量（sl/fhsl）；
工资 = 数量 × 单价（sl * price）；
工序进度 = 主单 + 明细 + 工序 + 已扫描条码合并后的完成进度。
**

**请明确订单、款号、计划、工单、工序、工价、员工、产量、工资和进度之间的对应关系。
生产计划接口:/api/NetYf/Plan/GridPageList
生产制单接口:/api/NetYf/Sclzd/GridPageList
对应关系大致为：
生产计划 → 生产制单 → 制单明细；
明细上挂：货号（huohao）、工序（worktype）、床号（chuanghao）；
员工通过 uid 与产量、工资关联；
产量 = 实际扫描/录入数量（sl/fhsl）；
工资 = 数量 × 单价（sl * price）；
工序进度 = 主单 + 明细 + 工序 + 已扫描条码合并后的完成进度。**

**如果一名员工同时参与多个订单，MES 返回的数据能否区分其在不同订单中的产量和工资？
**

**如果一名员工同时参与多个订单，MES 返回的数据能否区分其在不同订单中的产量和工资？**

**MES 是否可以提供业务状态、组织类型、工序、单位等基础字典，以及工资、产量、进度和排名的统一业务口径？
已提供基础字典接口：货号、工序、生产类型、员工等。
货号信息单据接口:/api/NetYf/Baseinfo/HuohaoQuery
货号信息表单接口:/api/NetYf/Baseinfo/HuohaoFormQuery
生产类型接口:/api/NetYf/Baseinfo/ScTypeQuery
生产工序接口:/api/NetYf/Baseinfo/RfidWorktypeQuery
货号工序接口:/api/NetYf/Baseinfo/HuohaoWorktypeQuery
员工信息接口:/api/NetYf/Baseinfo/EmployeeQuery
**

**MES 是否可以提供业务状态、组织类型、工序、单位等基础字典，以及工资、产量、进度和排名的统一业务口径？
已提供基础字典接口：货号、工序、生产类型、员工等。
货号信息单据接口:/api/NetYf/Baseinfo/HuohaoQuery
货号信息表单接口:/api/NetYf/Baseinfo/HuohaoFormQuery
生产类型接口:/api/NetYf/Baseinfo/ScTypeQuery
生产工序接口:/api/NetYf/Baseinfo/RfidWorktypeQuery
货号工序接口:/api/NetYf/Baseinfo/HuohaoWorktypeQuery
员工信息接口:/api/NetYf/Baseinfo/EmployeeQuery**

**对于没有专用接口，但能够通过多个 MES 只读 API 得到答案的问题，是否允许智能体平台自主组合、关联和分析？
允许。当前 MES 提供的都是只读查询接口
**

**对于没有专用接口，但能够通过多个 MES 只读 API 得到答案的问题，是否允许智能体平台自主组合、关联和分析？
允许。当前 MES 提供的都是只读查询接口**

**当无数据、参数错误或用户无权限时，MES 是否能够返回明确结果，便于智能体追问或调整查询？
当前接口统一返回 { code, msg, data, total } 结构：
无数据：code=0，data 为空数组；
参数错误：返回异常信息及明确错误消息；
权限不足：按配置过滤后通常表现为无数据
**

**当无数据、参数错误或用户无权限时，MES 是否能够返回明确结果，便于智能体追问或调整查询？
当前接口统一返回 { code, msg, data, total } 结构：
无数据：code=0，data 为空数组；
参数错误：返回异常信息及明确错误消息；
权限不足：按配置过滤后通常表现为无数据**

### 三、定时任务与事件推送


 1. 定时推送只面向已经登录并订阅的用户，还是需要覆盖客户全部员工？智能体平台是否可以保存用户映射和订阅信息？
 2. 当前 MES 未实现用户订阅机制
 3. 如果需要向从未登录过智能体平台的员工推送，MES 是否可以提供员工同步或其他用户获取方式？
 4. MES 可提供员工信息查询（/api/NetYf/Baseinfo/EmployeeQuery）
 5. 定时任务运行时用户不在线，智能体平台应以什么身份访问 MES，并如何按照目标用户的最新权限查询数据？
 6. 当前 MES 内部任务调度器未启用。
 7. 工资发布后，MES 是否可以通知智能体平台？采用事件推送还是由智能体平台定时查询？是否还需要生产日报、订单延期等其他通知？
 8. 当前 MES 暂无主动事件推送机制。
 9. 最终需要支持哪些推送渠道？客户是否已有统一消息发送能力？工资通知是否只提示登录查看，不直接展示金额？
 10. 当前 MES 无主动推送能力，需由智能体平台对接客户已有的消息渠道

### 五、请 MES 提供的资料


 1. 现有接口清单及支持状态。
 2. 统一身份认证及权限说明。
 3. 用户、角色和组织关系说明。
 4. 订单、款号、工单、工序和工价等业务关系说明。
 5. 产量、进度、工资和排名的业务口径。
 6. 接口文档及基本调用示例。
 7. 定时任务、事件通知和消息渠道说明。
 8. 测试环境、测试账号、联调计划。

**Query**
