"""Deterministic Mock MES dataset (Story 5): customer-shaped business objects.

``(scenario, seed, virtual_now)`` fully determines all data. The mock
simulates the customer's *interface shape and row-level filtering behavior*
(M3/M19); its business numbers are our deterministic fixtures, never real
customer data.

Object relationships follow V2 section 1.5 / M18:
生产计划 → 生产制单 → 制单明细（id = 物料编号，挂货号/工序/床号）；
员工经 uid 关联产量与工资；工资三源（扫码/吊挂/手工账）je = sl × price 恒等。
Organization is single-level workshops (M5/K2): no group tier, no history.
"""

from __future__ import annotations

import hashlib
import json
import random
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import Decimal
from typing import Literal, Sequence

Scenario = Literal["small", "standard"]
Record = dict[str, object]

#: Deterministic test identities covering the three filter tiers (M19):
#: company isolation, dept filtering, and move_admin_role="00" own-data-only.
IDENTITIES: dict[str, Record] = {
    # Full-factory identity: sees every company/dept row.
    "boss-a": {
        "app_key": "APPKEY-A",
        "user": "01009",
        "uname": "模拟厂长",
        "company": "COMPANY-A",
        "dept": None,
        "move_admin_role": "01",
    },
    # Workshop manager: sees only their workshop's rows.
    "manager-a": {
        "app_key": "APPKEY-A",
        "user": "01008",
        "uname": "模拟车间主任",
        "company": "COMPANY-A",
        "dept": "dept-a1",
        "move_admin_role": "02",
    },
    # Own-data-only worker (move_admin_role="00").
    "worker-a1": {
        "app_key": "APPKEY-A",
        "user": "01001",
        "uname": "模拟员工甲",
        "company": "COMPANY-A",
        "dept": "dept-a1",
        "move_admin_role": "00",
    },
    # Second company: proves company-level isolation.
    "worker-b1": {
        "app_key": "APPKEY-B",
        "user": "02001",
        "uname": "乙厂员工",
        "company": "COMPANY-B",
        "dept": "dept-b1",
        "move_admin_role": "00",
    },
}

APP_KEY_TO_COMPANY = {
    "APPKEY-A": "COMPANY-A",
    "APPKEY-B": "COMPANY-B",
}


@dataclass(frozen=True, slots=True)
class Dataset:
    scenario: Scenario
    seed: int
    virtual_now: datetime
    employees: list[Record]
    depts: list[Record]
    plans: list[Record]
    sclzd: list[Record]
    sclzd_worktypes: list[Record]
    barcodes: list[Record]
    barcode_cl: list[Record]
    pin_feng: list[Record]
    ysk: list[Record]
    wsk: list[Record]
    dg_cl: list[Record]
    huohao: list[Record]
    sc_types: list[Record]
    rfid_worktypes: list[Record]
    huohao_worktypes: list[Record]
    dg: list[Record]
    dg_zu: list[Record]
    user_info: list[Record]
    move_menu: list[Record]

    def digest(self) -> str:
        payload = {key: getattr(self, key) for key in self.__dataclass_fields__}
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode()
        return hashlib.sha256(encoded).hexdigest()


def _d(value: object) -> Decimal:
    return Decimal(str(value))


def build_dataset(
    scenario: Scenario = "small",
    seed: int = 20260821,
    virtual_now: datetime = datetime(2026, 8, 21, 8, tzinfo=timezone.utc),
) -> Dataset:
    """Build the deterministic dataset; same inputs yield the identical hash."""
    generator = random.Random(seed)  # nosec B311 - reproducible fixtures

    depts: list[Record] = [
        {
            "id": "dept-a1",
            "name": "一车间",
            "remark": "",
            "name_pk": "YCJ",
            "isdelete": False,
            "sysdept": "本厂",
            "company": "COMPANY-A",
            "companyName": "模拟服装厂A",
            "pid": "0",
            # Story 7: the dept field lets DeptQuery respect the M19 dept tier
            # so a workshop manager only resolves their own workshop.
            "dept": "dept-a1",
        },
        {
            "id": "dept-a2",
            "name": "二车间",
            "remark": "",
            "name_pk": "ECJ",
            "isdelete": False,
            "sysdept": "本厂",
            "company": "COMPANY-A",
            "companyName": "模拟服装厂A",
            "pid": "0",
            "dept": "dept-a2",
        },
        {
            "id": "dept-b1",
            "name": "乙厂车间",
            "remark": "",
            "name_pk": "YCCJ",
            "isdelete": False,
            "sysdept": "本厂",
            "company": "COMPANY-B",
            "companyName": "模拟服装厂B",
            "pid": "0",
            "dept": "dept-b1",
        },
    ]

    employees: list[Record] = [
        {
            "uid": "01001",
            "uname": "模拟员工甲",
            "name_pk": "MNYGJ",
            "mobile": "13800000001",
            "movepassword": "mock-pwd-01001",
            "move_Login": "1",
            "dept": "dept-a1",
            "deptname": "一车间",
            "employeeRule": '["0001"]',
            "move_scan": 0,
            "loginUserName": "",
            "zr_ck": "",
            "dy_gongzhong": "",
            "move_admin_role": "00",
            "company": "COMPANY-A",
        },
        {
            "uid": "01002",
            "uname": "模拟员工甲",  # 同名员工 edge case
            "name_pk": "MNYGJ",
            "mobile": "13800000002",
            "movepassword": "mock-pwd-01002",
            "move_Login": "1",
            "dept": "dept-a2",
            "deptname": "二车间",
            "employeeRule": '["0001"]',
            "move_scan": 1,
            "loginUserName": "",
            "zr_ck": "",
            "dy_gongzhong": "",
            "move_admin_role": "00",
            "company": "COMPANY-A",
        },
        {
            "uid": "01008",
            "uname": "模拟车间主任",
            "name_pk": "MNCJZR",
            "mobile": "13800000008",
            "movepassword": "mock-pwd-01008",
            "move_Login": "1",
            "dept": "dept-a1",
            "deptname": "一车间",
            "employeeRule": '["0002"]',
            "move_scan": 0,
            "loginUserName": "",
            "zr_ck": "",
            "dy_gongzhong": "",
            "move_admin_role": "02",
            "company": "COMPANY-A",
        },
        {
            "uid": "01009",
            "uname": "模拟厂长",
            "name_pk": "MNCZ",
            "mobile": "13800000009",
            "movepassword": "mock-pwd-01009",
            "move_Login": "1",
            "dept": "dept-a1",
            "deptname": "一车间",
            "employeeRule": '["0003"]',
            "move_scan": 0,
            "loginUserName": "",
            "zr_ck": "",
            "dy_gongzhong": "",
            "move_admin_role": "01",
            "company": "COMPANY-A",
        },
        {
            "uid": "02001",
            "uname": "乙厂员工",
            "name_pk": "YCYG",
            "mobile": "13900000001",
            "movepassword": "mock-pwd-02001",
            "move_Login": "1",
            "dept": "dept-b1",
            "deptname": "乙厂车间",
            "employeeRule": '["0001"]',
            "move_scan": 0,
            "loginUserName": "",
            "zr_ck": "",
            "dy_gongzhong": "",
            "move_admin_role": "00",
            "company": "COMPANY-B",
        },
    ]

    user_info: list[Record] = [
        {
            "code": "Admin",
            "username": "admin",
            "realname": "模拟管理员",
            "companyName": "模拟服装厂A",
        },
    ]
    move_menu: list[Record] = [
        {
            "uid": "01001",
            "uname": "模拟员工甲",
            "dept": "dept-a1",
            "menus": [{"name": "扫码产量", "model": "scan", "isScan": True, "sort": 1}],
        }
    ]

    huohao: list[Record] = [
        {
            "bh": "HH001",
            "bbreed": "模拟款A",
            "name_pk": "MNKA",
            "description": "模拟品名A",
            "stype": "T1",
            "huohaotype": "外套",
            "dw": "件",
            "lpinpai": "模拟品牌",
            "isdelete": False,
            "jst_huohao": "JST-HH001",
        },
        {
            "bh": "HH002",
            "bbreed": "模拟款B",
            "name_pk": "MNKB",
            "description": "模拟品名B",
            "stype": "T1",
            "huohaotype": "外套",
            "dw": "件",
            "lpinpai": "模拟品牌",
            "isdelete": False,
            "jst_huohao": "JST-HH002",
        },
    ]
    sc_types: list[Record] = [
        {
            "bh": "SC1",
            "name": "大身",
            "name_pk": "DS",
            "sfjcj": True,
            "isdelete": False,
            "sfcprk": False,
        },
    ]
    rfid_worktypes: list[Record] = [
        {
            "bh": f"WT{index:02d}",
            "name": name,
            "name_pk": name_pk,
            "gxtype": 0,
            "isdelete": False,
            "section": "缝制",
            "jc": name[:1],
            "sc_type": f"SYS-WT{index:02d}",
            "worktype_group": "缝制组",
            "yfgs": 2,
            "default_price": price,
            "gongzi_js_type": 1,
            "wt_sort": index * 10,
            "xz_price": _d(price) * 2,
            "default_working_hours": "0.50",
            "vehicle_type": "平车",
        }
        for index, (name, name_pk, price) in enumerate(
            [
                ("平车", "PC", "1.2500"),
                ("手工钉扣", "SGDK", "0.8000"),
                ("吊挂平车", "DGPC", "1.0000"),
            ],
            start=1,
        )
    ]
    huohao_worktypes: list[Record] = [
        {
            "id": f"hw-{huohao}-{wt}",
            "huohao": huohao,
            "huohaoname": "模拟款A" if huohao == "HH001" else "模拟款B",
            "wt": wt,
            "wtname": wt_name,
            "sort": sort,
            "sctype": "SC1",
            "sctypename": "大身",
            "sfzb": 0,
            "using_state": 1,
            "zhgx": 1 if sort == total else 0,
            "sfxs": 1,
            "theoretical_work_hours": "0.50",
        }
        for huohao in ("HH001", "HH002")
        for sort, (wt, wt_name) in enumerate(
            [("WT01", "平车"), ("WT02", "手工钉扣"), ("WT03", "吊挂平车")], start=1
        )
        for total in (3,)
    ]

    # ------------------------------------------------------ 计划与制单（多订单/同款多订单）
    plans: list[Record] = [
        {
            "dh": "PLAN-2607-001",
            "zhdate": "2026-07-01",
            "finish_date": "2026-07-31",
            "jhdh": "JH-2607-001",
            "hth": "HT-07-001",
            "gdy": "模拟跟单员",
            "zdr": "admin",
            "zsl": "100",
            "zdr_sh": "admin",
            "state": 1,
            "id": "plan-guid-1",
            "khddh": "KHDD-07-001",
            "pinpai": "P1",
            "pinpainame": "模拟品牌",
            "khid": "K001",
            "khname": "模拟客户",
            "khhh": "KHHH-07-001",
            "huohao": "HH001",
            "huohaoname": "模拟款A",
            "spname": "模拟品名A",
            "color": "黑色",
            "chima": "M",
            "dw": "件",
            "ddsl": "100",
            "paol": "2",
            "sl": "98",
            "remark": "",
            "company": "COMPANY-A",
            "dept": "dept-a1",
        },
        {
            # 延期订单：finish_date 早于 virtual_now（FR-009 基础高亮用例）
            "dh": "PLAN-2608-001",
            "zhdate": "2026-08-01",
            "finish_date": "2026-08-05",
            "jhdh": "JH-2608-001",
            "hth": "HT-08-001",
            "gdy": "模拟跟单员",
            "zdr": "admin",
            "zsl": "50",
            "zdr_sh": "admin",
            "state": 1,
            "id": "plan-guid-2",
            "khddh": "KHDD-08-001",
            "pinpai": "P1",
            "pinpainame": "模拟品牌",
            "khid": "K001",
            "khname": "模拟客户",
            "khhh": "KHHH-08-001",
            "huohao": "HH001",
            "huohaoname": "模拟款A",
            "spname": "模拟品名A",
            "color": "白色",
            "chima": "L",
            "dw": "件",
            "ddsl": "50",
            "paol": "1",
            "sl": "49",
            "remark": "",
            "company": "COMPANY-A",
            "dept": "dept-a2",
        },
        {
            # 零计划 edge case
            "dh": "PLAN-2608-002",
            "zhdate": "2026-08-10",
            "finish_date": "2026-09-30",
            "jhdh": "JH-2608-002",
            "hth": "HT-08-002",
            "gdy": "模拟跟单员",
            "zdr": "admin",
            "zsl": "0",
            "zdr_sh": "admin",
            "state": 0,
            "id": "plan-guid-3",
            "khddh": "KHDD-08-002",
            "pinpai": "P1",
            "pinpainame": "模拟品牌",
            "khid": "K002",
            "khname": "模拟客户二",
            "khhh": "KHHH-08-002",
            "huohao": "HH002",
            "huohaoname": "模拟款B",
            "spname": "模拟品名B",
            "color": "蓝色",
            "chima": "S",
            "dw": "件",
            "ddsl": "0",
            "paol": "0",
            "sl": "0",
            "remark": "零计划",
            "company": "COMPANY-A",
            "dept": "dept-a1",
        },
        {
            "dh": "PLAN-B-001",
            "zhdate": "2026-08-01",
            "finish_date": "2026-09-15",
            "jhdh": "JH-B-001",
            "hth": "HT-B-001",
            "gdy": "乙厂跟单员",
            "zdr": "admin",
            "zsl": "30",
            "zdr_sh": "admin",
            "state": 1,
            "id": "plan-guid-b1",
            "khddh": "KHDD-B-001",
            "pinpai": "P2",
            "pinpainame": "乙厂品牌",
            "khid": "KB01",
            "khname": "乙厂客户",
            "khhh": "KHHH-B-001",
            "huohao": "HH002",
            "huohaoname": "模拟款B",
            "spname": "模拟品名B",
            "color": "红色",
            "chima": "M",
            "dw": "件",
            "ddsl": "30",
            "paol": "0",
            "sl": "30",
            "remark": "",
            "company": "COMPANY-B",
            "dept": "dept-b1",
        },
    ]

    sclzd: list[Record] = []
    sclzd_worktypes: list[Record] = []
    barcodes: list[Record] = []
    barcode_cl: list[Record] = []
    pin_feng: list[Record] = []
    ysk: list[Record] = []
    wsk: list[Record] = []
    dg_cl: list[Record] = []

    # 扫码记录（Story 7 fixture）：worker-a1 与 worker-a2 各自的产量；进度一致性
    # 由 uid 非空决定。detail id 必须与下方 sclzd 的物料编号一致（Story 7 通过
    # param_bindings 用 sclzd.id 派生 WorktypeProgressQuery.userid）。
    scan_plan = [
        # (detail id, plan dh, uid, uname, dept, worktype, qty, price, day)
        (
            1001,
            "ZD-2607-001",
            "01001",
            "模拟员工甲",
            "dept-a1",
            "WT01",
            "4",
            "1.2500",
            "2026-07-31",
        ),
        (
            1001,
            "ZD-2608-001",
            "01001",
            "模拟员工甲",
            "dept-a1",
            "WT01",
            "5",
            "1.2500",
            "2026-08-05",
        ),
        (
            1001,
            "ZD-2608-001",
            "01001",
            "模拟员工甲",
            "dept-a1",
            "WT03",
            "4",
            "1.0000",
            "2026-08-06",
        ),
        (
            1002,
            "ZD-2608-001",
            "01002",
            "模拟员工甲",
            "dept-a2",
            "WT01",
            "3",
            "1.2500",
            "2026-08-20",
        ),
        (1003, "ZD-B-001", "02001", "乙厂员工", "dept-b1", "WT01", "6", "1.2500", "2026-08-20"),
    ]
    # 制单语境完成量 sssl = 该物料已扫码产量之和（M18/1.5 分语境表）。
    sssl_by_detail: dict[int, int] = {}
    for did, _dh, _uid, _uname, _dept, _wt, qty, _price, _day in scan_plan:
        sssl_by_detail[did] = sssl_by_detail.get(did, 0) + int(qty)

    detail_id = 1000
    for plan in plans:
        company = str(plan["company"])
        dept = str(plan["dept"])
        prefix = "ZD-B" if company == "COMPANY-B" else "ZD"
        dh = str(plan["dh"]).replace("PLAN", prefix)
        fhsl_total = int(str(plan["sl"]))
        if fhsl_total <= 0:
            continue
        detail_id += 1
        sclzd.append(
            {
                "dh": dh,
                "zhdate": str(plan["zhdate"]),
                "dddh": str(plan["jhdh"]),
                "khid": str(plan["khid"]),
                "khname": str(plan["khname"]),
                "drdg_status": 0,
                "huohao": str(plan["huohao"]),
                "huohaoname": str(plan["huohaoname"]),
                "description": str(plan["spname"]),
                "sctype": "SC1",
                "sctypename": "大身",
                "chuanghao": "床号1",
                "cjr": "模拟裁剪员",
                "zdr": "admin",
                "state": 1,
                "id": str(detail_id),
                "baohao": "包1",
                "ganghao": "缸1",
                "color": str(plan["color"]),
                "chima": str(plan["chima"]),
                "fhsl": str(fhsl_total),
                "sssl": str(sssl_by_detail.get(detail_id, 0)),
                "remark": "",
                "company": company,
                "dept": dept,
            }
        )
        for sort, (wt, wt_name) in enumerate(
            [("WT01", "平车"), ("WT02", "手工钉扣"), ("WT03", "吊挂平车")], start=1
        ):
            sclzd_worktypes.append(
                {
                    "id": f"sw-{detail_id}-{wt}",
                    "dh": dh,
                    "huohao": str(plan["huohao"]),
                    "huohaoname": str(plan["huohaoname"]),
                    "wt": wt,
                    "wtname": wt_name,
                    "sort": sort,
                    "zhgx": 1 if sort == 3 else 0,
                    "sfzb": 0,
                    "sctype": "SC1",
                    "sctypename": "大身",
                    "company": company,
                    "dept": dept,
                }
            )

    _wtname_by_wt = {"WT01": "平车", "WT02": "手工钉扣", "WT03": "吊挂平车"}
    for did, dh, uid, uname, dept, wt, qty, price, day in scan_plan:
        company = "COMPANY-B" if dept == "dept-b1" else "COMPANY-A"
        je = _d(qty) * _d(price)
        record_base = {
            "inputtime": f"{day} 09:30:00",
            "uid": uid,
            "uname": uname,
            "dept": dept,
            "deptname": next(d["name"] for d in depts if d["id"] == dept),
            "rq": day,
            "chuanghao": "床号1",
            "sctype": "SC1",
            "sctypename": "大身",
            "baohao": "包1",
            "id": str(did),
            "huohao": "HH001" if company == "COMPANY-A" else "HH002",
            "bbreed": "模拟款A" if company == "COMPANY-A" else "模拟款B",
            "description": "模拟品名A" if company == "COMPANY-A" else "模拟品名B",
            "color": "黑色" if company == "COMPANY-A" else "红色",
            "chima": "M",
            "worktype": wt,
            "wtname": _wtname_by_wt[wt],
            "fhsl": "98",
            "price": price,
            "company": company,
        }
        barcode_cl.append(
            {
                **record_base,
                "sssl": qty,
                "sl": "98",
                "je": str(je),
            }
        )
        ysk.append(
            {
                **record_base,
                "inputtime_raw": f"{day} 09:30:00.000",
                "sl": qty,
                "je": str(je),
                "cid": f"cid-{did}-{wt}",
                "sffb": 0,
                "fbid": "",
            }
        )
        barcodes.append(
            {
                "dh": dh,
                "detailId": did,
                "uid": uid,
                "uname": uname,
                "dept": dept,
                "worktype": wt,
                "inputtime": f"{day} 09:30:00",
                "company": company,
            }
        )
        if wt == "WT03":
            dg_cl.append(
                {
                    "id": f"dgc-{did}-{uid}",
                    "rq": day,
                    "dddh": "JH-2608-001",
                    "chuanghao": "床号1",
                    "huohao": "HH001",
                    "bbreed": "模拟款A",
                    "color": "黑色",
                    "chima": "M",
                    "worktype": wt,
                    "wtname": "吊挂平车",
                    "uid": uid,
                    "uname": uname,
                    "dguid": f"D{uid}",
                    "dguname": f"吊挂{uname}",
                    "dept": dept,
                    "dgName": "一号吊挂线",
                    "dgStyleNo": "1",
                    "sl": qty,
                    "price": price,
                    "je": str(je),
                    "sfjz": 0,
                    "company": company,
                }
            )

    # 手工账：唯一含次品 cp 的来源（C.5）；je = sl × price 恒等。
    pin_feng.append(
        {
            "dh": "PF-2608-001",
            "zhdate": "2026-08-06",
            "state": 1,
            "zhuser": "admin",
            "zhuser_sh": "admin",
            "id": "pf-1",
            "dept": "dept-a1",
            "deptname": "一车间",
            "uid": "01001",
            "uname": "模拟员工甲",
            "huohao": "HH001",
            "huohaoname": "模拟款A",
            "ddh": "JH-2608-001",
            "worktype": "WT02",
            "wtname": "手工钉扣",
            "dw": "件",
            "js": "3",
            "sl": "3",
            "cp": "1",
            "chuanghao": "床号1",
            "color": "黑色",
            "chima": "M",
            "price": "0.8000",
            "je": str(_d("3") * _d("0.8000")),
            "remark": "",
            "company": "COMPANY-A",
        }
    )

    # 未扫描：总工序减去已扫工序后的余量（与 WorktypeProgressQuery 自洽）。
    wsk.append(
        {
            "id": "1001",
            "chuanghao": "床号1",
            "huohao": "HH001",
            "color": "黑色",
            "chima": "M",
            "worktype": "WT02",
            "sl": "93",
            "baohao": "包1",
            "company": "COMPANY-A",
            "dept": "dept-a1",
        }
    )

    dg: list[Record] = [
        {
            "id": "dg1",
            "dg_type": "模拟吊挂公司",
            "dg_name": "一号吊挂线",
            "dg_Server": "127.0.0.1",
            "dg_Database": "mock_dg",
            "dg_Uid": "mock_user",
            "dg_Pwd": "mock-dg-password",  # nosec B105 - fixture value only
            "company": "COMPANY-A",
        }
    ]
    dg_zu: list[Record] = [
        {
            "id": "dgz1",
            "dgname": "一号吊挂线",
            "xianhao": "1",
            "zuBieName": "一组",
            "company": "COMPANY-A",
        }
    ]

    dataset = Dataset(
        scenario=scenario,
        seed=seed,
        virtual_now=virtual_now,
        employees=employees,
        depts=depts,
        plans=plans,
        sclzd=sclzd,
        sclzd_worktypes=sclzd_worktypes,
        barcodes=barcodes,
        barcode_cl=barcode_cl,
        pin_feng=pin_feng,
        ysk=ysk,
        wsk=wsk,
        dg_cl=dg_cl,
        huohao=huohao,
        sc_types=sc_types,
        rfid_worktypes=rfid_worktypes,
        huohao_worktypes=huohao_worktypes,
        dg=dg,
        dg_zu=dg_zu,
        user_info=user_info,
        move_menu=move_menu,
    )

    if scenario == "standard":
        _extend_standard(dataset, generator)
    return dataset


def _extend_standard(dataset: Dataset, generator: random.Random) -> None:
    """Standard scenario: generated extra rows across months and worktypes."""
    base = [row for row in dataset.barcode_cl if row["company"] == "COMPANY-A"]
    for index in range(20):
        source = dict(base[index % len(base)])
        quantity = generator.randint(1, 8)
        day = f"2026-08-{(index % 20) + 1:02d}"
        source["inputtime"] = f"{day} 12:00:00"
        source["rq"] = day
        source["uid"] = "01001" if index % 2 == 0 else "01002"
        source["uname"] = "模拟员工甲"
        source["dept"] = "dept-a1" if index % 2 == 0 else "dept-a2"
        source["deptname"] = "一车间" if index % 2 == 0 else "二车间"
        je = _d(quantity) * _d(source["price"])
        source["sssl"] = str(quantity)
        source["je"] = str(je)
        dataset.barcode_cl.append(source)

        ysk_row = {
            **source,
            "inputtime_raw": f"{day} 12:00:00.000",
            "sl": str(quantity),
            "je": str(je),
            "cid": f"cid-std-{index}",
            "sffb": 0,
            "fbid": "",
        }
        dataset.ysk.append(ysk_row)


def reset_database_stub() -> None:
    """Backward-compatible no-op; the in-memory mock needs no database reset."""
    return None


def main(argv: Sequence[str] | None = None) -> None:
    import argparse

    from mock_mes.config import get_settings

    settings = get_settings()
    parser = argparse.ArgumentParser(description="Build a deterministic Mock MES dataset")
    parser.add_argument("--scenario", choices=("small", "standard"), default=settings.scenario)
    parser.add_argument("--seed", type=int, default=settings.seed)
    parser.add_argument("--virtual-now", default=settings.virtual_now.isoformat())
    args = parser.parse_args(argv)
    virtual_now = datetime.fromisoformat(args.virtual_now.replace("Z", "+00:00"))
    dataset = build_dataset(args.scenario, args.seed, virtual_now)
    print(
        json.dumps(
            {
                "scenario": dataset.scenario,
                "seed": dataset.seed,
                "virtual_now": dataset.virtual_now.isoformat(),
                "dataset_hash": dataset.digest(),
            },
            sort_keys=True,
        )
    )


if __name__ == "__main__":
    main()
