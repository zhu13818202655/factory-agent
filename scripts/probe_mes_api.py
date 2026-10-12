"""MES V2 接口手工探测脚本（独立运行，不读任何外部文件）。

用法：
    1. 在下面 ``MES_BASE_URL`` 和 ``CREDENTIALS`` 里填好实际值；
    2. 在文件底部的 ``main`` 里选择调 ``gongzi_cl_query`` 或 ``scjd_query``，
       角色、参数直接写在函数调用里；
    3. 直接运行：``uv run python scripts/probe_mes_api.py``

每次调用前脚本会自动做 token 交换（POST /api/system/token），
不用管 token，只管业务参数。
"""

from __future__ import annotations

import json
import time
from typing import Any, cast

import httpx

# ←←← 填 MES 基础地址（token 与业务接口共用） ←←←
MES_BASE_URL = "http://hzlinkbiz.ywhzsoft.com:9002"

# ←←← 填各角色的加密凭据（token 接口的 app_key 原文） ←←←
CREDENTIALS: dict[str, str] = {
    "00": "5eVIhChLn9xNxWlU5SwYIzDRWPvQmzGmi92IxoYqem5ozPnLp9ldeHlO4YwHdQIls8n9emVm4ybyt3fNr5XJj6BIIOU8aLo7Gq6SgoomY3X2YFbKmKxGTcjR6hnqk07o",  # noqa: E501  # 员工
    "01": "5eVIhChLn9xNxWlU5SwYIzDRWPvQmzGmi92IxoYqem5ozPnLp9ldeHlO4YwHdQIl/+aeODsXCgKP9quMdWxVMKBIIOU8aLo7Gq6SgoomY3X2YFbKmKxGTcjR6hnqk07o",  # noqa: E501  # 组长
    "02": "5eVIhChLn9xNxWlU5SwYIzDRWPvQmzGmi92IxoYqem5ozPnLp9ldeHlO4YwHdQIlGuo2BbiiwKfO5egnAcMMbeAwsqDgcOO/fQdy0rV7+fH2YFbKmKxGTcjR6hnqk07o",  # noqa: E501  # 管理
    "99": "5eVIhChLn9xNxWlU5SwYIzDRWPvQmzGmi92IxoYqem5ozPnLp9ldeHlO4YwHdQIlx7G1Dh+bj107KnXuWQBoScchWWxEwyp+wuuVcqzEsvNc4gjZr+h5brQVbeROEHRi",  # noqa: E501  # 老板
}

_TOKEN_PATH = "/api/system/token"
_TIMEOUT_SECONDS = 150.0

# 两个 V2 接口路径
_CL_PATH = "/api/NetYf/Sclzd/v2/GongziClQuery"
_SCJD_PATH = "/api/NetYf/Sclzd/v2/ScjdQuery"


def gongzi_cl_query(role: str = "99", **params: Any) -> None:
    """V2 工资产量查询（result 为裸数组，不分页一次性全量返回）。

    params 原样透传，全部可选字段（docs/api/工资产量-V2.md §3）：
        dates:         开始日期 "2026-09-01"（不传默认本月 1 日）
        datee:         结束日期 "2026-09-30"（不传默认今天）
        uid:           员工工号（等值），如 "001"
        Flag:          "0"=按录入时间；"1"=按审核时间且已审核；其他/空=未审核
        Type:          工资类型逗号拼接："0"扫码 / "1"吊挂 / "2"手工账，如 "0,1,2"
        bbreed:        货号**名称**（自动转编号过滤），如 "纯棉T恤"
        wtname:        工序**名称**（自动转编号过滤），如 "裁剪"
        chuanghao:     床号（模糊匹配）
        groupbyFields: 不传/空 = 明细模式（走缓存的就是这种）；传入则按维度汇总。
                       可选值：inputdate / jhdh / chuanghao / huohao / bbreed /
                       type / worktype / wtname / price / uid / uname / dept /
                       deptname / color / chima
        orderbyFields: 排序，如 ["je:desc", "chuanghao:asc"]
        page/size/queryFooter 未生效，传了也忽略。
    """
    _call(role, _CL_PATH, params)


def scjd_query(role: str = "99", **params: Any) -> None:
    """V2 生产进度查询（result 为裸数组）。

    params 原样透传，全部可选字段（docs/api/生产进度-V2.md §3）：
        dates:         开始日期（必填，流程卡制单日期）"2026-09-01"
        datee:         结束日期（必填，流程卡制单日期）"2026-09-30"
        ---- 过滤参数（都是可选）----
        dh:            制单号（等值），如 "ZD202609001"
        jhdh:          计划号/订单号（等值）
        chuanghao:     床号（模糊）
        huohao:        货号编码（等值）
        bbreed:        货号名称（等值）
        worktype:      工序编码（等值）
        wtname:        工序名称（等值）
        color:         颜色（模糊）
        chima:         尺码（模糊）
        uid:           员工工号（等值）
        dept:          部门编码（等值），如 "D001"
        ---- 动态分组 ----
        groupbyFields: 不传 = 默认明细分组（全字段）；传则按维度汇总，输出列=传入顺序。
                       可选值：dh / jhdh / chuanghao / huohao / bbreed /
                       worktype / wtname / color / chima / baohao / userid /
                       fhsl / sssl / uid / uname / dept / deptname / wsort / zhgx
        ---- 排序 ----
        orderbyFields: 每项 "字段" 或 "字段:asc|desc"，如 ["wcl:desc"]
        orderByType:   全局方向 "asc"（默认）/ "desc"
    """
    _call(role, _SCJD_PATH, params)


def _call(role: str, path: str, params: dict[str, Any]) -> None:
    """换 token → 拼请求体 → POST → 打印结果。业务参数原样透传。"""
    credential = CREDENTIALS.get(role, "")
    if not MES_BASE_URL:
        raise SystemExit("先在脚本顶部填 MES_BASE_URL")
    if not credential:
        raise SystemExit(f"先在脚本顶部 CREDENTIALS 里填角色 {role} 的凭据")

    with httpx.Client(base_url=MES_BASE_URL, timeout=_TIMEOUT_SECONDS) as client:
        # 1. 自动换 token（公共参数 app_key/timestamp/sign 都来自它的返回）
        token_body = {"app_key": credential}
        response = client.post(_TOKEN_PATH, json=token_body)
        response.raise_for_status()
        token_result = cast(
            "dict[str, Any]", response.json().get("result") or {}
        )
        print(
            f"token ok  role={role}  roles={token_result.get('roles')}"
            f"  dept={token_result.get('dept')}"
        )

        # 2. 业务参数平铺顶层 + 公共参数
        body: dict[str, Any] = dict(params)
        body["app_key"] = token_result.get("appkey")
        body["timestamp"] = token_result.get("timestamp")
        body["sign"] = token_result.get("sign")

        # 3. 调业务接口（鉴权 = Header Bearer accessToken + body 公共参数，缺一不可）
        started = time.monotonic()
        print(f"POST {path}  body={json.dumps(body, ensure_ascii=False)[:300]}")
        response = client.post(
            path,
            json=body,
            headers={"Authorization": f"Bearer {token_result.get('accessToken')}"},
        )
        elapsed_ms = (time.monotonic() - started) * 1000

    print(f"POST {path}  http={response.status_code}  {elapsed_ms:.0f}ms")
    if response.status_code != 200:
        print("non-200 body:", response.text[:500])
        raise SystemExit(1)

    envelope = cast("dict[str, Any]", response.json())
    print(f"code={envelope.get('code')}  message={envelope.get('message')!r}")

    result: Any = envelope.get("result")
    if not isinstance(result, list):
        print(
            "result 不是数组:",
            json.dumps(result, ensure_ascii=False, default=str)[:500],
        )
        return
    rows = cast("list[Any]", result)
    print(f"rows: {len(rows)}")
    if rows and isinstance(rows[0], dict):
        print("columns:", list(cast("dict[str, Any]", rows[0]).keys()))
    for row in rows[:5]:
        print(json.dumps(row, ensure_ascii=False, default=str)[:300])


def main() -> None:
    # ↓↓↓ 自己改这里：调哪个接口、用哪个角色、传什么参数 ↓↓↓
    # 想加/删参数看上面两个函数的 docstring，字段全列在里面了。
    gongzi_cl_query(
        role="99",
        dates="2026-09-01",
        datee="2026-09-30",
        Flag="0",
        Type="0,1,2",
        # groupbyFields="uid,uname",          # 实验：字符串形态
        groupbyFields=["uid", "uname"],     # 对照：数组形态
        # uid="001",                        # 指定查某个员工
        # bbreed="纯棉T恤",                 # 货号名称（不是编号）
        # wtname="裁剪",                    # 工序名称
        # chuanghao="床号关键字",           # 床号模糊匹配
    )
    # scjd_query(
    #     role="99",
    #     dates="2026-09-01",
    #     datee="2026-09-30",
    #     # dh="ZD202609001",              # 指定制单号
    #     # jhdh="JH2609001",              # 指定计划号/订单号
    #     # huohao="H2601",                # 货号编码
    #     # bbreed="纯棉T恤",              # 货号名称
    #     # chuanghao="2",                 # 床号（模糊）
    #     # worktype="GX001",              # 工序编码
    #     # wtname="裁剪",                 # 工序名称
    #     # color="黑色",                  # 颜色（模糊）
    #     # chima="L",                     # 尺码（模糊）
    #     # uid="001",                     # 员工工号
    #     # dept="D001",                   # 部门编码
    #     # groupbyFields=["dept", "deptname", "huohao", "bbreed"],
    #     # orderbyFields=["wcl:desc"],    # 按完工率降序
    #     # orderByType="desc",
    # )


if __name__ == "__main__":
    main()
