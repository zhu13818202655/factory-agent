"""MES 直连调试台接口目录（tools 专用，非产品代码，不随发布打包）。

数据来源：
- 客户接口文档 `docs/product/AI问答对外接口-整理.md`（参数含义/类型/可选值）
- `tools/direct_mes_client.py`（可用默认值、请求体平铺约定、queryFooter 布尔约定）

约定（与 direct_mes_client.py 保持一致）：
- 业务参数与 app_key/timestamp/sign 同层平铺，由 server.py 注入鉴权三参。
- 空字符串 / None 参数发送前剔除；queryFooter 一律以布尔发送。
- GongziMxQuery 的 Uid 留空时自动回填为当前登录用户工号（autofill: "uid"）。
- scheme 为空 = 明细；有值（hz/HZ/汇总）= 汇总。HuohaoWtCLQuery 的 scheme 另为
  「货号工序 / 工序」汇总粒度，二者含义不同，勿混用。
- GongziClQuery（工资产量查询 v2）不分页（page/size 未生效），汇总用 groupbyFields /
  orderbyFields 字符串数组参数（kind: "list"）：前端以逗号分隔输入、发送时转为 JSON 数组。
- ScjdQueryV2（缝制生产进度 v2）合并了原「缝制生产进度 / 进度详情 / 工序进度」三个接口，
  同样用 groupbyFields / orderbyFields 控制分组与排序，另有全局排序方向 orderByType。
  完工率 wcl 口径固定为 实收(sssl)/发卡(fhsl)，不随分组维度变化。
- 部分接口的成功 code 不一致（多数为 1，个别为 200），由 endpoint 的 successCodes 声明，
  页面按该列表判定成功/失败；错误文案兼容 message / msg 两种字段。
- 客户新出的 V2 接口单独放在「V2 联调（新接口）」分组，与既有接口隔离；
  联调稳定后可再归入业务分组。
"""

from typing import Any


def _param(
    name: str,
    label: str,
    *,
    kind: str = "text",
    ptype: str = "string",
    required: bool = False,
    default: Any = "",
    placeholder: str = "",
    hint: str = "",
    options: list[dict[str, str]] | None = None,
    autofill: str | None = None,
) -> dict[str, Any]:
    return {
        "name": name,
        "label": label,
        "kind": kind,  # text | int | bool | select | list（逗号分隔输入 → JSON 数组）
        "type": ptype,  # 接口文档中的类型（仅作提示展示）
        "required": required,
        "default": default,
        "placeholder": placeholder,
        "hint": hint,
        "options": options or [],
        "autofill": autofill,
    }


# ---------------------------------------------------------------------------
# 常用分页 / 日期参数构造（与 direct_mes_client 顶部常量一致）
# ---------------------------------------------------------------------------
DATE_HINT = "格式 YYYY-MM-DD"


def _page() -> dict[str, Any]:
    return _param(
        "page",
        "页码",
        kind="int",
        ptype="integer",
        required=True,
        default=1,
        hint="当前页，从 1 开始",
    )


def _size() -> dict[str, Any]:
    return _param(
        "size",
        "每页数量",
        kind="int",
        ptype="integer",
        required=True,
        default=200,
        hint="每页条数（200 以内较稳；值过大或窗口过长时 MES 响应可能较慢）",
    )


def _dates() -> dict[str, Any]:
    return _param(
        "dates",
        "开始日期",
        ptype="string",
        required=True,
        default="2026-07-01",
        placeholder="2026-07-01",
        hint=DATE_HINT,
    )


def _datee() -> dict[str, Any]:
    return _param(
        "datee",
        "结束日期",
        ptype="string",
        required=True,
        default="2026-07-31",
        placeholder="2026-07-31",
        hint=DATE_HINT,
    )


def _query_footer() -> dict[str, Any]:
    return _param(
        "queryFooter",
        "是否返回合计",
        kind="bool",
        ptype="boolean",
        required=True,
        default=True,
        hint="客户实测必须以布尔 true/false 发送；true 时响应多返回 result.footer",
    )


def _sfwg() -> dict[str, Any]:
    return _param(
        "sfwg",
        "是否完工",
        kind="select",
        required=True,
        default="",
        options=[
            {"value": "", "label": "全部", "emptyLabel": "全部（空=不发送）"},
            {"value": "0", "label": "0：未完工"},
            {"value": "1", "label": "1：已完工"},
        ],
        hint=(
            '客户口径：""=全部 / 0=未完工 / 1=已完工。本调试台统一剔除空值参数，'
            '故「全部」= 不发送 sfwg；若实测要求显式带空串，可用下方「附加任意 JSON 参数」补 {"sfwg": ""}。'
        ),
    )


def _fz_drill_params() -> list[dict[str, Any]]:
    """「缝制详细进度」「缝制工序总体进度」共用入参：计划号 + 货号 + 床号 + 日期。"""
    return [
        _param(
            "dddh",
            "计划号",
            required=True,
            placeholder="可留空（本厂多为空串）",
            hint="客户示例传空串；留空则不发送该参数 = 不加计划号过滤。",
        ),
        _param(
            "huohao",
            "货号",
            required=True,
            placeholder="如 00322",
            hint="货号编码（取自「缝制总进度」响应 list[].huohao）。",
        ),
        _param(
            "chuanghao",
            "床号",
            required=True,
            placeholder="如 1",
            hint="床号（取自「缝制总进度」响应 list[].chuanghao）。",
        ),
        _param(
            "rq",
            "日期",
            required=True,
            placeholder="如 2026-09-02",
            hint="日期（取自「缝制总进度」响应 list[].rq）；" + DATE_HINT + "。",
        ),
    ]


def _scjdiv2_filter_params() -> list[dict[str, Any]]:
    """ScjdQuery v2 的过滤参数（等价匹配为主，仅床号/颜色/尺码为模糊）。"""
    return [
        _param(
            "dh",
            "制单号",
            placeholder="如 ZD202609001",
            hint="流程卡制单号，等值匹配。",
        ),
        _param(
            "jhdh",
            "计划号/订单号",
            placeholder="如 JH202609001",
            hint="计划号 / 订单号（两者等价），等值匹配。",
        ),
        _param(
            "chuanghao",
            "床号",
            placeholder="如 1",
            hint="床号，模糊匹配。",
        ),
        _param(
            "huohao",
            "货号编码",
            placeholder="如 00063",
            hint="货号编码，等值匹配（传编码，不是款号名称）。",
        ),
        _param(
            "bbreed",
            "货号名称",
            placeholder="如 G2601",
            hint="货号名称，等值匹配（传名称，不是货号编码）。",
        ),
        _param(
            "worktype",
            "工序编码",
            placeholder="如 01",
            hint="工序编码，等值匹配。",
        ),
        _param(
            "wtname",
            "工序名称",
            placeholder="如 印标",
            hint="工序名称，等值匹配。",
        ),
        _param(
            "color",
            "颜色",
            placeholder="如 黑色",
            hint="颜色，模糊匹配。",
        ),
        _param(
            "chima",
            "尺码",
            placeholder="如 L",
            hint="尺码，模糊匹配。",
        ),
        _param(
            "uid",
            "员工工号",
            placeholder="选填：留空按角色范围",
            hint=(
                "扫码员工工号，等值匹配。注意本接口参数名是小写 uid（与 GongziMxQuery 的 Uid 不同）；"
                "留空则不发送，由 MES 按角色数据范围返回（00 员工禁止查询 / 01 组长本部门 / "
                "02 管理配置部门范围 / 其他·管理员 全部）。"
            ),
        ),
        _param(
            "dept",
            "部门编码",
            placeholder="如 D001",
            hint=(
                "扫码员工所属部门编码，等值匹配。01/02 角色会被服务端自动收窄到权限范围内的部门，"
                "显式传越权部门无效。"
            ),
        ),
    ]


def _scjdiv2_group_params() -> list[dict[str, Any]]:
    """ScjdQuery v2 的动态分组 / 排序参数。"""
    return [
        _param(
            "groupbyFields",
            "动态分组",
            kind="list",
            ptype="string[]",
            placeholder="留空=明细全字段；如 dept,deptname,huohao,bbreed",
            hint=(
                "决定输出列集合、列顺序与行排序优先级（多个用英文逗号分隔，发送时自动转 JSON 数组）。"
                "不传/空 = 默认明细分组（全字段，等效原「详情+工序」合并视图，"
                "含未扫码工序行 uid/uname 为 null、wgs=0）。"
                "重复字段自动去重；编码列与名称列属同一维度（如 huohao+bbreed）同时传入只分组一次、输出两列。"
                "可选：dh 制单号、jhdh 计划号/订单号、chuanghao 床号、huohao 货号编码、bbreed 货号名称、"
                "worktype 工序编码、wtname 工序名称、color 颜色、chima 尺码、baohao 包号、"
                "userid 流程卡物料编号、fhsl 发卡数量、sssl 实收数量、uid 员工工号、uname 员工姓名、"
                "dept 部门编码、deptname 部门名称、wsort 工序排序号、zhgx 最后工序（1=最后工序）。"
                "实测服务端还接受 dddh（文档未列）；传不可用字段会返回 code=0 并回列可选集。"
            ),
        ),
        _param(
            "orderbyFields",
            "排序项",
            kind="list",
            ptype="string[]",
            placeholder="如 wcl:desc,zpsl:desc",
            hint=(
                "每项格式 \"字段\" 或 \"字段:asc|desc\"，按传入顺序决定优先级"
                "（多个用英文逗号分隔，发送时自动转 JSON 数组）。"
                "可选排序字段 = 所有分组输出列 + 聚合输出列 + wcl / bgs / bts；"
                "不传时：自定义分组按分组字段顺序升序，默认明细分组按 chuanghao → huohao → worktype 升序；"
                "传入不存在的字段会返回明确错误提示。"
            ),
        ),
        _param(
            "orderByType",
            "全局排序方向",
            kind="select",
            ptype="string",
            default="",
            options=[
                {"value": "", "label": "asc（默认）", "emptyLabel": "asc（默认，空=不发送）"},
                {"value": "asc", "label": "asc：升序"},
                {"value": "desc", "label": "desc：降序"},
            ],
            hint=(
                "作用于 orderbyFields 中未单独指定方向的项（单项写成 \"字段:desc\" 时该项方向优先）。"
                "留空 = 不发送 = 服务端默认 asc。"
            ),
        ),
    ]


def _endpoint(
    key: str,
    name: str,
    path: str,
    *,
    auth: bool,
    desc: str,
    params: list[dict[str, Any]] | None = None,
    note: str = "",
    success_codes: tuple[int, ...] = (1,),
) -> dict[str, Any]:
    return {
        "key": key,
        "name": name,
        "path": path,
        "auth": auth,  # 是否需要自动获取 token 并注入三参 + Authorization
        "desc": desc,
        "note": note,
        "params": params or [],
        "successCodes": list(success_codes),  # 判定「成功」的业务 code（客户各接口不统一）
    }


CATALOG: list[dict[str, Any]] = [
    # ------------------------------------------------------------------ 认证
    {
        "group": "认证",
        "items": [
            _endpoint(
                "token",
                "获取认证信息",
                "/api/system/token",
                auth=False,
                desc="换取 accessToken / sign / timestamp，是所有业务请求的前提",
                note="认证接口本身：服务端用所选角色凭据换取 token；页面不展示任何鉴权参数。",
            ),
            _endpoint(
                "TestPermissions",
                "测试权限",
                "/api/print/test-permissions",
                auth=True,
                desc="验证当前认证信息是否有效",
                note="仅携带鉴权三参，无业务参数；调用前会自动获取 token。",
            ),
        ],
    },
    # -------------------------------------------------------------- 基础数据
    {
        "group": "基础数据",
        "items": [
            _endpoint(
                "UserInfoQuery",
                "用户信息查询",
                "/api/NetYf/Baseinfo/UserInfoQuery",
                auth=True,
                desc="查询登录用户信息",
                params=[
                    _param(
                        "USERNAME",
                        "登录用户名",
                        required=True,
                        placeholder="admin",
                        hint="登录用户名（示例 admin）；留空则不发送，通常返回当前登录用户。",
                    )
                ],
            ),
            _endpoint(
                "MoveMenuQuery",
                "用户菜单查询",
                "/api/NetYf/Baseinfo/MoveMenuQuery",
                auth=True,
                desc="查询当前登录用户的移动端菜单",
            ),
            _endpoint(
                "HuohaoQuery",
                "货号信息单据",
                "/api/NetYf/Baseinfo/HuohaoQuery",
                auth=True,
                desc="查询货号及货号类型字典",
                note="客户文档：本接口无业务参数，返回全部货号与货号类型。",
            ),
            _endpoint(
                "HuohaoFormQuery",
                "货号信息表单",
                "/api/NetYf/Baseinfo/HuohaoFormQuery",
                auth=True,
                desc="按货号查询颜色、尺码明细",
                params=[
                    _param(
                        "huohao",
                        "货号编号",
                        required=True,
                        placeholder="00001",
                        hint=(
                            "货号编号（返回该货号的颜色/尺码明细）。必填："
                            "客户实测留空会返回 code=0「货号不能为空」。"
                        ),
                    )
                ],
            ),
            _endpoint(
                "ScTypeQuery",
                "生产类型",
                "/api/NetYf/Baseinfo/ScTypeQuery",
                auth=True,
                desc="查询生产类型字典（整件 / 大身 …）",
            ),
            _endpoint(
                "RfidWorktypeQuery",
                "生产工序",
                "/api/NetYf/Baseinfo/RfidWorktypeQuery",
                auth=True,
                desc="查询生产工序字典",
            ),
            _endpoint(
                "HuohaoWorktypeQuery",
                "货号工序",
                "/api/NetYf/Baseinfo/HuohaoWorktypeQuery",
                auth=True,
                desc="按货号查询该货号绑定的工序",
                params=[
                    _param(
                        "huohao",
                        "货号编号",
                        placeholder="00001",
                        hint=(
                            "货号编号（返回该货号的工序明细）。选填：留空时服务端不发送该参数，"
                            "即不过滤，返回全部货号绑定的工序（客户实测 8,690 行 / 约 2.9 MB）。"
                        ),
                    )
                ],
                note="客户文档标注 huohao 必填，实测留空即不过滤、返回全量（响应可能很大）。",
            ),
            _endpoint(
                "EmployeeQuery",
                "员工信息",
                "/api/NetYf/Baseinfo/EmployeeQuery",
                auth=True,
                desc="查询员工信息",
                params=[
                    _param(
                        "uid",
                        "员工工号",
                        placeholder="选填：留空返回角色范围",
                        hint=(
                            "员工工号；选填——留空则不发送该参数，由 MES 按调用者数据范围返回"
                            "（本厂实测条数：00 员工 1 / 01 组长 2210 / 02 管理 2303 / 99 老板 2600）。"
                        ),
                    )
                ],
            ),
            _endpoint(
                "DeptQuery",
                "部门信息",
                "/api/NetYf/Baseinfo/DeptQuery",
                auth=True,
                desc="查询部门信息",
            ),
        ],
    },
    # -------------------------------------------------------------- 生产计划
    {
        "group": "生产计划",
        "items": [
            _endpoint(
                "PlanGridPageList",
                "生产计划",
                "/api/NetYf/Plan/GridPageList",
                auth=True,
                desc="按日期范围分页查询生产计划",
                params=[_page(), _size(), _dates(), _datee()],
            ),
        ],
    },
    # -------------------------------------------------------------- 生产制单
    {
        "group": "生产制单",
        "items": [
            _endpoint(
                "SclzdGridPageList",
                "生产制单",
                "/api/NetYf/Sclzd/GridPageList",
                auth=True,
                desc="按日期范围分页查询生产制单",
                params=[_page(), _size(), _dates(), _datee()],
            ),
            _endpoint(
                "SclzdWorktypeQuery",
                "生产制单工序",
                "/api/NetYf/Sclzd/SclzdWorktypeQuery",
                auth=True,
                desc="按单号查询制单工序",
                params=[
                    _param(
                        "dh",
                        "单号",
                        required=True,
                        placeholder="如 202608010001",
                        hint="制单单号（从「生产制单」响应 list[].dh 获取）。",
                    )
                ],
            ),
            _endpoint(
                "SclzdBarcodeQuery",
                "生产制单工序扫描",
                "/api/NetYf/Sclzd/SclzdBarcodeQuery",
                auth=True,
                desc="按单号和物料编号查询扫描记录",
                params=[
                    _param(
                        "dh",
                        "单号",
                        required=True,
                        placeholder="如 202608160017",
                        hint="制单单号（从「生产制单」响应 list[].dh 获取）。",
                    ),
                    _param(
                        "detailId",
                        "物料编号",
                        kind="int",
                        ptype="integer",
                        required=True,
                        default="",
                        placeholder="如 95715",
                        hint="物料编号（从「生产制单」响应 list[].id 获取）。",
                    ),
                ],
            ),
        ],
    },
    # -------------------------------------------------------------- 产量与产能
    {
        "group": "产量与产能",
        "items": [
            _endpoint(
                "BarcodeClQuery",
                "线下工序产量产能",
                "/api/NetYf/Sclzd/BarcodeClQuery",
                auth=True,
                desc="分页查询线下扫码产量明细",
                params=[_page(), _size(), _dates(), _datee()],
            ),
            _endpoint(
                "HuohaoWtCLQuery",
                "工序产量查询",
                "/api/NetYf/Sclzd/HuohaoWtCLQuery",
                auth=True,
                desc="按货号工序 / 工序汇总查询产量",
                params=[
                    _page(),
                    _size(),
                    _query_footer(),
                    _dates(),
                    _datee(),
                    _param(
                        "scheme",
                        "汇总方式",
                        kind="select",
                        ptype="string",
                        required=True,
                        default="货号工序",
                        options=[
                            {"value": "货号工序", "label": "货号工序（按货号+工序）"},
                            {"value": "工序", "label": "工序（仅按工序）"},
                        ],
                        hint="本接口 scheme 是汇总粒度（与工资明细的 scheme 含义不同）。",
                    ),
                    _param(
                        "huohao",
                        "款号",
                        placeholder="86B",
                        hint=(
                            "按款号过滤。注意取值是款号 bbreed（如 86B），"
                            "不是货号流水号 bh——客户实测该区间 72 个 huohao 唯一值 72/72 命中 bbreed。"
                        ),
                    ),
                    _param(
                        "worktype",
                        "工序名称",
                        placeholder="印标",
                        hint="按工序名称过滤，取值对应生产工序 name（如 印标 / 裁剪）。",
                    ),
                ],
            ),
            _endpoint(
                "WorktypeProgressQuery",
                "工序进度查询",
                "/api/NetYf/Sclzd/WorktypeProgressQuery",
                auth=True,
                desc="按物料编号查询各工序进度",
                params=[
                    _page(),
                    _size(),
                    _param(
                        "userid",
                        "物料编号",
                        ptype="integer",
                        required=True,
                        placeholder="如 4496",
                        hint="物料编号。文档示例按字符串发送，故默认按字符串提交。",
                    ),
                    _param(
                        "uid",
                        "员工工号",
                        placeholder="选填：留空不过滤",
                        hint="员工工号；选填——留空则不发送该参数（按调用者数据范围/不过滤）。",
                    ),
                ],
            ),
        ],
    },
    # -------------------------------------------------------------- 生产查询
    {
        "group": "生产查询",
        "items": [
            _endpoint(
                "YskQuery",
                "生产查询-已扫描",
                "/api/NetYf/Sclzd/YskQuery",
                auth=True,
                desc="分页查询已扫描产量记录",
                params=[
                    _page(),
                    _size(),
                    _dates(),
                    _datee(),
                    _param(
                        "Uid",
                        "员工工号",
                        placeholder="选填：留空按角色范围",
                        hint=(
                            "员工工号；选填。填工号 = 查该员工已扫描记录；"
                            "留空则不发送该参数，由 MES 按调用者数据范围返回。"
                        ),
                    ),
                ],
                note=(
                    "Uid 选填：产品侧 apis.yaml 的 required_params 也只列 dates / datee（Uid 记 scope）；"
                    "省略 Uid 时 MES 实际返回范围本厂尚未单独实测。"
                ),
            ),
            _endpoint(
                "WskQuery",
                "生产查询-未扫描",
                "/api/NetYf/Sclzd/WskQuery",
                auth=True,
                desc="分页查询未扫描产量记录",
                params=[_page(), _size(), _dates(), _datee()],
            ),
        ],
    },
    # -------------------------------------------------------------- 缝制进度
    {
        "group": "缝制进度",
        "items": [
            _endpoint(
                "SclzdScjdQuery",
                "缝制生产进度",
                "/api/NetYf/Sclzd/ScjdQuery",
                auth=True,
                desc="按日期范围分页查询流程卡层缝制进度",
                params=[
                    _page(),
                    _size(),
                    _dates(),
                    _datee(),
                    _sfwg(),
                    _param(
                        "searchKeyword",
                        "关键词",
                        placeholder="床号 / 货号简称 / 计划号",
                        hint="模糊过滤：床号 / 货号简称 / 计划号；留空则不发送该参数。",
                    ),
                ],
            ),
            _endpoint(
                "SclzdScjdDetailQuery",
                "缝制生产进度详情",
                "/api/NetYf/Sclzd/ScjdDetailQuery",
                auth=True,
                desc="按流程卡单号查询包级缝制进度",
                params=[
                    _dates(),
                    _datee(),
                    _param(
                        "dh",
                        "流程卡单号",
                        required=True,
                        placeholder="如 202609010001",
                        hint="流程卡单号（取自「缝制生产进度」响应 result.main[].dh）。",
                    ),
                ],
                note="响应 result.list[] 一行 = 一个包；list[].id 即「缝制工序进度」的 userid 入参。",
            ),
            _endpoint(
                "SclzdScjdGxQuery",
                "缝制工序进度",
                "/api/NetYf/Sclzd/ScjdGxQuery",
                auth=True,
                desc="按物料编号查询工序刷卡进度（本接口无分页）",
                params=[
                    _param(
                        "userid",
                        "物料编号",
                        ptype="integer",
                        required=True,
                        placeholder="如 45139",
                        hint="物料编号（取自「缝制生产进度详情」响应 list[].id）。",
                    ),
                    _param(
                        "uid",
                        "员工工号",
                        placeholder="选填：留空不过滤",
                        hint="员工工号；选填（客户示例传空串），留空则不发送该参数。",
                    ),
                ],
                note=(
                    "返回字段与「工序进度查询」（WorktypeProgressQuery）逐字段一致，差别是本接口不带 page / size。"
                    "客户文档示例不自洽（请求 userid=45139、响应 userid=4496，且示例数据与 WorktypeProgressQuery 相同），以实测为准。"
                ),
            ),
            _endpoint(
                "SclzdScjdFzHzQuery",
                "缝制总进度",
                "/api/NetYf/Sclzd/ScjdFzHzQuery",
                auth=True,
                desc="按日期范围分页查询床号层缝制完工率",
                params=[
                    _page(),
                    _size(),
                    _dates(),
                    _datee(),
                    _param(
                        "searchKeyword",
                        "关键词",
                        placeholder="计划号 / 货号 / 床号 / 品名",
                        hint="模糊过滤：计划号 / 货号 / 床号 / 品名；留空则不发送该参数。",
                    ),
                ],
                note=(
                    "响应 result.list[] 一行 = 一个「计划号 + 货号 + 床号 + 日期」："
                    "fzzsl 缝制总数量 / wgzsl 完工总数量 / wgl 完工率；"
                    "下钻用「缝制详细进度」「缝制工序总体进度」（拿本接口的 dddh + huohao + chuanghao + rq）。"
                ),
            ),
            _endpoint(
                "SclzdScjdFzMxQuery",
                "缝制详细进度",
                "/api/NetYf/Sclzd/ScjdFzMxQuery",
                auth=True,
                desc="按计划号/货号/床号/日期查询颜色尺码层完工率",
                params=_fz_drill_params(),
                note="响应 result.list[] 一行 = 一个「颜色 + 尺码」；sssl 数量 / wgsl 完工 / wgl 完工率。",
            ),
            _endpoint(
                "SclzdScjdFzHzWorktypeQuery",
                "缝制工序总体进度",
                "/api/NetYf/Sclzd/ScjdFzHzWorktypeQuery",
                auth=True,
                desc="按计划号/货号/床号/日期查询工序层完成汇总",
                params=_fz_drill_params(),
                note=(
                    "响应 result 除 list 外还平铺返回 wtname / sssl / sksl / wsksl 四个平行数组（可直接喂图）。"
                    "客户示例三行 sksl+wsksl 恒为 220 而 sssl 为 214/214/220，sssl 语义须实测确认。"
                ),
            ),
        ],
    },
    # ------------------------------------------------------------------ 工资
    {
        "group": "工资",
        "items": [
            _endpoint(
                "GongziMxQuery",
                "工资明细/汇总查询",
                "/api/NetYf/Sclzd/GongziMxQuery",
                auth=True,
                desc="查询工资明细或汇总（线下 + 吊挂 + 手工账）",
                params=[
                    _page(),
                    _size(),
                    _query_footer(),
                    _dates(),
                    _datee(),
                    _param(
                        "Uid",
                        "员工工号",
                        autofill="uid",
                        placeholder="选填：留空按角色范围",
                        hint=(
                            "员工工号；选填（客户确认 2026-09-06）。填工号 = 查该员工；"
                            "留空 = 由 MES 按调用者角色范围返回（00 员工仅本人 / 01 组长、02 管理 本管辖范围 / 99 全厂）。"
                            "勾选「自动」时留空将由服务端回填当前登录工号（= 查本人）。"
                        ),
                    ),
                    _param(
                        "Flag",
                        "日期口径",
                        kind="select",
                        ptype="string",
                        required=True,
                        default="0",
                        options=[
                            {"value": "0", "label": "0：按扫描日期"},
                            {"value": "1", "label": "1：按审核日期"},
                        ],
                        hint="工资按哪种日期口径统计。",
                    ),
                    _param(
                        "Type",
                        "工资类型",
                        kind="select",
                        ptype="string",
                        required=True,
                        default="0,1,2",
                        options=[
                            {"value": "0", "label": "0：扫码产量"},
                            {"value": "1", "label": "1：吊挂产量"},
                            {"value": "2", "label": "2：手工账产量"},
                            {"value": "0,1", "label": "0,1：扫码+吊挂"},
                            {"value": "0,2", "label": "0,2：扫码+手工账"},
                            {"value": "1,2", "label": "1,2：吊挂+手工账"},
                            {"value": "0,1,2", "label": "0,1,2：三类全部"},
                        ],
                        hint="可多选，用英文逗号分隔（0 扫码 / 1 吊挂 / 2 手工账）。",
                    ),
                    _param(
                        "scheme",
                        "明细 / 汇总",
                        kind="select",
                        ptype="string",
                        required=True,
                        default="",
                        options=[
                            {"value": "", "label": "明细（scheme 为空）"},
                            {"value": "hz", "label": "汇总（hz）"},
                            {"value": "HZ", "label": "汇总（HZ）"},
                            {"value": "汇总", "label": "汇总（汇总）"},
                        ],
                        hint="为空查明细；hz / HZ / 汇总 均可触发汇总（客户三种写法等价）。",
                    ),
                ],
                note=(
                    "Uid 选填：勾选「自动」且留空 = 服务端回填登录工号（查本人）；"
                    "不勾选且留空 = 完全不发送 Uid，由 MES 按角色范围返回"
                    "（对应 apis.yaml 里 Uid 记 scope、不在 required_params）。"
                ),
            ),
            _endpoint(
                "GongziJeOrderQuery",
                "员工工资排名查询",
                "/api/NetYf/Sclzd/GongziJeOrderQuery",
                auth=True,
                desc="按日期范围查询员工工资排名",
                params=[_page(), _size(), _query_footer(), _dates(), _datee()],
            ),
        ],
    },
    # ---------------------------------------------------------------- 手工账
    {
        "group": "手工账",
        "items": [
            _endpoint(
                "PinFengGridPageList",
                "手工账单据",
                "/api/NetYf/PinFeng/GridPageList",
                auth=True,
                desc="分页查询手工账单据",
                params=[_page(), _size(), _dates(), _datee()],
            ),
        ],
    },
    # -------------------------------------------------------------- 吊挂对接
    {
        "group": "吊挂对接",
        "items": [
            _endpoint(
                "DgGridPageList",
                "吊挂中间库配置",
                "/api/NetYf/Dg/GridPageList",
                auth=True,
                desc="查询吊挂中间库连接配置",
                params=[_page(), _size()],
            ),
            _endpoint(
                "DgZuGridPageList",
                "吊挂组别",
                "/api/NetYf/Dg/DgZuGridPageList",
                auth=True,
                desc="查询吊挂线别与组别",
                params=[_page(), _size()],
            ),
            _endpoint(
                "DgClQuery",
                "吊挂工序产能",
                "/api/NetYf/Dg/DgClQuery",
                auth=True,
                desc="分页查询吊挂产量明细",
                params=[_page(), _size(), _dates(), _datee()],
            ),
        ],
    },
    # -------------------------------------------------------------- V2 联调
    {
        "group": "V2 联调（新接口）",
        "items": [
            _endpoint(
                "GongziClQuery",
                "工资产量查询(明细/汇总)",
                "/api/NetYf/Sclzd/v2/GongziClQuery",
                auth=True,
                desc="查询员工工资/产量，支持明细与按维度汇总（一次性全量返回，不分页）",
                params=[
                    _param(
                        "dates",
                        "开始日期",
                        placeholder="留空 = 默认本月 1 日",
                        hint=DATE_HINT + "；选填——留空则不发送，服务端默认本月 1 日。",
                    ),
                    _param(
                        "datee",
                        "结束日期",
                        placeholder="留空 = 默认今天",
                        hint=DATE_HINT + "；选填——留空则不发送，服务端默认今天。",
                    ),
                    _param(
                        "uid",
                        "员工工号",
                        placeholder="选填：留空按角色范围",
                        hint=(
                            "员工工号。注意本接口参数名是小写 uid（与 GongziMxQuery 的 Uid 不同）；"
                            "选填——留空则不发送，由 MES 按调用者角色数据范围返回"
                            "（00 员工仅本人 / 01 组长本部门 / 02 管理管辖部门 / 99 老板全部），"
                            "请求体里越权的 uid 会被服务端忽略。"
                        ),
                    ),
                    _param(
                        "Flag",
                        "审核口径",
                        kind="select",
                        ptype="string",
                        required=False,
                        default="",
                        options=[
                            {"value": "", "label": "未审核（空/其他=未审核）", "emptyLabel": "未审核（空=不发送）"},
                            {"value": "0", "label": "0：按录入时间"},
                            {"value": "1", "label": "1：按审核时间且已审核"},
                        ],
                        hint=(
                            "本接口语义：0=按录入时间；1=按审核时间且已审核；其他/空=未审核。"
                            "与 GongziMxQuery 的「日期口径」含义不同，勿混用。"
                        ),
                    ),
                    _param(
                        "Type",
                        "工资类型",
                        kind="select",
                        ptype="string",
                        required=False,
                        default="0,1,2",
                        options=[
                            {"value": "0", "label": "0：扫码产量"},
                            {"value": "1", "label": "1：吊挂产量"},
                            {"value": "2", "label": "2：手工账产量"},
                            {"value": "0,1", "label": "0,1：扫码+吊挂"},
                            {"value": "0,2", "label": "0,2：扫码+手工账"},
                            {"value": "1,2", "label": "1,2：吊挂+手工账"},
                            {"value": "0,1,2", "label": "0,1,2：三类全部"},
                        ],
                        hint="工资类型，逗号拼接多选（0 扫码 / 1 吊挂 / 2 手工账）。",
                    ),
                    _param(
                        "bbreed",
                        "货号名称",
                        placeholder="如 纯棉T恤",
                        hint=(
                            "按货号名称过滤（注意传名称如「纯棉T恤」，不是货号编号），"
                            "服务端自动转货号编号过滤。"
                        ),
                    ),
                    _param(
                        "wtname",
                        "工序名称",
                        placeholder="如 裁剪",
                        hint="按工序名称过滤（服务端自动转工序编号）。",
                    ),
                    _param(
                        "chuanghao",
                        "床号",
                        placeholder="如 1",
                        hint="床号，模糊匹配。",
                    ),
                    _param(
                        "groupbyFields",
                        "汇总维度",
                        kind="list",
                        ptype="string[]",
                        placeholder="留空=明细；如 uid,uname,deptname",
                        hint=(
                            "不传/空 = 明细模式；传入则按维度汇总（多个用英文逗号分隔，发送时自动转 JSON 数组）。"
                            "可选：inputdate、jhdh(计划号/订单号)、chuanghao(床号)、huohao(货号编码)、"
                            "bbreed(货号名称)、type(工资类型)、worktype(工序编码)、wtname(工序名称)、price、"
                            "uid(员工工号)、uname(员工姓名)、dept(部门编码)、deptname(部门名称)、color、chima。"
                        ),
                    ),
                    _param(
                        "orderbyFields",
                        "排序项",
                        kind="list",
                        ptype="string[]",
                        placeholder="如 je:desc,chuanghao:asc",
                        hint="排序项，格式 字段:asc/desc（多个用英文逗号分隔，发送时自动转 JSON 数组）。",
                    ),
                ],
                note=(
                    "本接口不分页（page/size 当前版本未生效，传了也忽略），符合条件的数据一次性全量返回。"
                    "响应 list 列 = groupbyFields（明细模式用默认列），名称列已自动翻译"
                    "（bbreed 货号名 / wtname 工序名 / uname 员工姓名 / deptname 部门名）；"
                    "footer 合计只含 sum/count 聚合列（列名 = 聚合别名 + _total，avg/max/min 不参与）。"
                ),
            ),
            _endpoint(
                "ScjdQueryV2",
                "缝制生产进度 V2（明细/汇总）",
                "/api/NetYf/Sclzd/v2/ScjdQuery",
                auth=True,
                desc=(
                    "合并原「缝制生产进度 / 进度详情 / 工序进度」三个接口，"
                    "支持动态分组（groupbyFields）与动态排序"
                ),
                success_codes=(1, 200),
                params=[
                    _dates(),
                    _datee(),
                    *_scjdiv2_filter_params(),
                    *_scjdiv2_group_params(),
                ],
                note=(
                    "完工率 wcl 口径固定为 实收 sssl / 发卡 fhsl × 100（保留 2 位小数，分母为 0 时返回 0），"
                    "不随分组维度变化、无需调用方指定；footer 的 wcl_total = sssl_total / fhsl_total × 100，"
                    "不是各行 wcl 的平均。纯制单级分组额外输出 bgs 已完工包数 / bts 总包数，"
                    "wgs 已完工工序数 / wts 总工序数同理，均仅为参考统计、不参与 wcl 计算。"
                    "权限按扫码记录（扫码员工所属部门）判定：00 员工禁止查询、01 组长仅本部门、"
                    "02 管理看配置部门范围、其他/管理员全部。"
                    "【2026-10-09 实测·与文档不一致处，以实测为准】"
                    "响应为 {code, message, result, timestamp}，成功 code=1"
                    "（文档写 code=200/msg/data，实测未出现，故成功码同时接受 1 与 200）；"
                    "错误 code=0（无效分组/排序字段，服务端会回列可选字段）/ -403（无权限），文案在 message。"
                    "分组字段可选集以服务端报错信息为准（实测含 dddh，文档未列）；"
                    "排序字段 = 分组输出列 + 聚合列 + wcl/bgs/bts。"
                    "实测 sssl 可大于 fhsl，wcl 因此会出现 >100（如 107.14），非脏数据。"
                ),
            ),
        ],
    },
]

#: 认证接口本身（无需注入三参 / Bearer）。
TOKEN_PATH = "/api/system/token"

#: 是否需要外部业务键的典型必填参数提示（供页面渲染 placeholder 校验使用）。
REQUIRED_EXAMPLE: dict[str, str] = {
    "USERNAME": "admin",
    "huohao": "00001",
    "dh": "202608010001",
    "detailId": "95715",
    "userid": "4496",
    "uid": "01001",
    "Uid": "001",
}


def as_json() -> list[dict[str, Any]]:
    return CATALOG
