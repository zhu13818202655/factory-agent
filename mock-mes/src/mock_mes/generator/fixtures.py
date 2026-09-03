"""Deterministic fixtures for the production-like generator.

Two layers:
- **Master data** (``master_rows``): tenant/employee/style/process tables,
  identical for every generated day, decided by the factory-scale settings.
- **Anchored business rows** (``anchored_rows``): the anchored fixtures
  (plans, production orders, scans, hanging/manual entries, unscanned rows)
  fixed to their historic dates so the customer-shaped numbers and the wages
  golden stay byte-identical.

All values are development fixtures, never real customer data.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from decimal import Decimal

from mock_mes.config import MockMesSettings

#: Deterministic holiday list used by the work calendar. Weekends plus
#: these dates are non-production days (no rolling output is generated).
HOLIDAYS: frozenset[date] = frozenset(
    {
        date(2025, 1, 1),
        date(2025, 1, 28),
        date(2025, 1, 29),
        date(2025, 1, 30),
        date(2025, 1, 31),
        date(2025, 2, 1),
        date(2025, 2, 2),
        date(2025, 2, 3),
        date(2025, 2, 4),
        date(2025, 5, 1),
        date(2025, 5, 2),
        date(2025, 5, 3),
        date(2025, 5, 4),
        date(2025, 5, 5),
        date(2025, 10, 1),
        date(2025, 10, 2),
        date(2025, 10, 3),
        date(2025, 10, 4),
        date(2025, 10, 5),
        date(2025, 10, 6),
        date(2025, 10, 7),
        date(2025, 10, 8),
        date(2026, 1, 1),
        date(2026, 2, 15),
        date(2026, 2, 16),
        date(2026, 2, 17),
        date(2026, 2, 18),
        date(2026, 2, 19),
        date(2026, 2, 20),
        date(2026, 2, 21),
        date(2026, 2, 22),
        date(2026, 2, 23),
        date(2026, 2, 24),
        date(2026, 5, 1),
        date(2026, 5, 2),
        date(2026, 5, 3),
        date(2026, 5, 4),
        date(2026, 5, 5),
        date(2026, 10, 1),
        date(2026, 10, 2),
        date(2026, 10, 3),
        date(2026, 10, 4),
        date(2026, 10, 5),
        date(2026, 10, 6),
        date(2026, 10, 7),
        date(2026, 10, 8),
    }
)

#: Anchor dates. Anchored rows only exist on these days; rolling
#: rows are generated on every production day of the window.
ANCHOR_PLAN_1 = date(2026, 7, 1)
ANCHOR_PLAN_2 = date(2026, 8, 1)
ANCHOR_PLAN_3 = date(2026, 8, 10)
ANCHOR_SCAN_1 = date(2026, 7, 31)
ANCHOR_SCAN_2 = date(2026, 8, 5)
ANCHOR_SCAN_3 = date(2026, 8, 6)
ANCHOR_SCAN_4 = date(2026, 8, 20)


@dataclass(frozen=True, slots=True)
class RowInsert:
    """One row to write into a ``mock_*`` table."""

    table: str
    payload: dict[str, object]
    #: Explicit primary-key value; defaults to the table's id key in payload.
    id: str | None = None
    company: str | None = None


@dataclass(frozen=True, slots=True)
class DayPlan:
    """Deterministic output of one generated day."""

    day: date
    inserts: list[RowInsert] = field(default_factory=list[RowInsert])
    #: (detail_id, new_cumulative_sssl) — sclzd progress updates (跨日连续).
    ssl_updates: list[tuple[str, Decimal]] = field(default_factory=list[tuple[str, Decimal]])


def _d(value: object) -> Decimal:
    return Decimal(str(value))


# ---------------------------------------------------------------------------
# Master data (identical for every day; decided by the factory-scale settings).
# ---------------------------------------------------------------------------

#: Role codes confirmed by the customer (four tiers):
#: 00 员工 (own data only) / 01 组长 (bound 小组 group, see ``group``) /
#: 02 管理 (bound 车间/部门 depts, possibly several) / 99 老板 (whole factory,
#: exactly one per company). Contract: docs/product/需求及方案整理.md 角色定义.
ROLE_WORKER = "00"
ROLE_GROUP_LEADER = "01"
ROLE_MANAGER = "02"
ROLE_BOSS = "99"

#: Group-id prefix separator used for employee group ids (``dept-a1-g01``).
_GROUP_SEP = "-g"


def group_id(dept: str, group_index: int) -> str:
    """Deterministic 小组 id for ``dept`` and a 1-based group index."""
    return f"{dept}{_GROUP_SEP}{group_index:02d}"


def _anchored_employee(
    uid: str,
    uname: str,
    dept: str,
    deptname: str,
    role: str,
    company: str,
    *,
    rule: str = '["0001"]',
    move_scan: int = 0,
    group: str = "",
) -> dict[str, object]:
    return {
        "uid": uid,
        "uname": uname,
        "name_pk": f"MN{uid}",
        "mobile": f"138000{uid}",
        "movepassword": f"mock-pwd-{uid}",
        "move_Login": "1",
        "dept": dept,
        "deptname": deptname,
        "employeeRule": rule,
        "move_scan": move_scan,
        "loginUserName": "",
        "zr_ck": "",
        "dy_gongzhong": "",
        "move_admin_role": role,
        "company": company,
        "group": group,
    }


#: Anchored employees keep their original payloads verbatim (role included),
#: so the customer-shaped fixtures, goldens and contract tests never drift.
#: Scale generation fills the rest of the headcount around them. 01009 is the
#: single factory boss (99) under the confirmed four-tier role codes.
#: ``group`` names the 小组 a 组长/员工 belongs to (empty for 管理/老板, who are
#: not production-group members).
_ANCHORED_EMPLOYEES: dict[str, dict[str, object]] = {
    "01001": _anchored_employee(
        "01001",
        "模拟员工甲",
        "dept-a1",
        "一车间",
        ROLE_WORKER,
        "COMPANY-A",
        group=group_id("dept-a1", 1),
    ),
    # 同名员工 edge case (same name, different workshop/uid).
    "01002": _anchored_employee(
        "01002",
        "模拟员工甲",
        "dept-a2",
        "二车间",
        ROLE_WORKER,
        "COMPANY-A",
        move_scan=1,
        group=group_id("dept-a2", 1),
    ),
    "01008": _anchored_employee(
        "01008",
        "模拟车间主任",
        "dept-a1",
        "一车间",
        ROLE_MANAGER,
        "COMPANY-A",
        rule='["0002"]',
    ),
    "01009": _anchored_employee(
        "01009", "模拟厂长", "dept-a1", "一车间", ROLE_BOSS, "COMPANY-A", rule='["0003"]'
    ),
    "02001": _anchored_employee(
        "02001",
        "乙厂员工",
        "dept-b1",
        "乙厂车间",
        ROLE_WORKER,
        "COMPANY-B",
        group=group_id("dept-b1", 1),
    ),
}

#: Common Chinese surnames and given names for realistic-looking staff.
_SURNAMES = (
    "王李张刘陈杨黄赵吴周徐孙马朱胡林郭何高罗郑梁谢宋唐许韩冯邓曹彭曾肖田董袁潘"
    "蒋蔡余杜叶程苏魏吕丁任沈姚卢姜崔钟谭陆汪范金石廖贾夏韦付方白邹孟熊秦邱江尹"
    "薛闫段雷侯龙史陶黎贺顾毛郝龚邵万钱严覃武戴莫孔向汤"
)
_GIVEN_NAMES = (
    "伟芳娜秀英敏静丽强磊军洋勇艳杰娟涛明超秀兰霞平刚桂英文辉建华玉兰萍鹏飞"
    "雪梅燕子红兵志强晓东国庆春花秀云小龙海燕国庆志明小红建华玉梅金凤秀珍"
    "桂芳春燕雪松海洋文强秀荣丽华桂英晓明大勇小燕子豪子轩浩然梓涵欣怡"
)


def _workshop_name(index: int) -> str:
    numerals = "一二三四五六七八九十"
    if index < 10:
        return f"{numerals[index]}车间"
    return f"第{index + 1}车间"


#: Style-name suffixes and garment kinds used for the generated catalogue.
_STYLE_SUFFIX = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"
_STYLE_KINDS = ("外套", "衬衫", "裤子", "连衣裙", "羽绒服", "针织衫", "夹克", "风衣")


def _dept_rows(settings: MockMesSettings) -> list[dict[str, object]]:
    rows: list[dict[str, object]] = []
    for index in range(settings.departments):
        dept_id = f"dept-a{index + 1}"
        rows.append(
            {
                "id": dept_id,
                "name": _workshop_name(index),
                "remark": "",
                "name_pk": f"CJ{index + 1:02d}",
                "isdelete": False,
                "sysdept": "本厂",
                "company": "COMPANY-A",
                "companyName": "模拟服装厂A",
                "pid": "0",
                "dept": dept_id,
            }
        )
    for index in range(settings.company_b_departments):
        dept_id = f"dept-b{index + 1}"
        rows.append(
            {
                "id": dept_id,
                "name": "乙厂车间" if index == 0 else _workshop_name(index),
                "remark": "",
                "name_pk": f"YCJ{index + 1:02d}",
                "isdelete": False,
                "sysdept": "本厂",
                "company": "COMPANY-B",
                "companyName": "模拟服装厂B",
                "pid": "0",
                "dept": dept_id,
            }
        )
    return rows


def _employee(
    uid: str,
    uname: str,
    dept: str,
    deptname: str,
    role: str,
    company: str,
    *,
    group: str = "",
    bound_depts: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "uid": uid,
        "uname": uname,
        "name_pk": f"EM{uid}",
        "mobile": f"13{uid}8",
        "movepassword": f"mock-pwd-{uid}",
        "move_Login": "1",
        "dept": dept,
        "deptname": deptname,
        "employeeRule": '["0001"]',
        "move_scan": 0,
        "loginUserName": "",
        "zr_ck": "",
        "dy_gongzhong": "",
        "move_admin_role": role,
        "company": company,
        "group": group,
        #: Manager-only: the full bound 车间/部门 list (own dept first). 组长/
        #: 员工/老板 never carry more than their own dept.
        "boundDepts": list(bound_depts),
    }


def _employee_rows(settings: MockMesSettings) -> list[dict[str, object]]:
    """Headcount split across departments, one manager and N leaders per dept.

    Composition per department (``per_dept = headcount / departments``):
    1 manager (02) + ``per_dept // group_size`` group leaders (01) + rest
    workers (00). The factory boss (99) is a single anchored employee.
    """
    rows: list[dict[str, object]] = []
    per_dept = max(settings.headcount // max(settings.departments, 1), 1)
    group_size = max(settings.group_size, 1)
    name_index = 0

    for dept_index in range(settings.departments):
        dept_id = f"dept-a{dept_index + 1}"
        dept_name = _workshop_name(dept_index)
        # Cross-workshop binding demo (客户确认：管理可跨车间绑定多部门): the
        # manager of the second workshop also binds the fourth workshop when
        # the factory is large enough to have one.
        bound_depts: tuple[str, ...] = ()
        if dept_index == 1 and settings.departments >= 4:
            bound_depts = (dept_id, "dept-a4")
        for position in range(per_dept):
            uid = f"01{dept_index * per_dept + position + 1:03d}"
            anchored = _ANCHORED_EMPLOYEES.get(uid)
            if anchored is not None:
                rows.append(dict(anchored))
                continue
            surname = _SURNAMES[name_index % len(_SURNAMES)]
            given = _GIVEN_NAMES[(name_index * 7) % len(_GIVEN_NAMES)]
            name_index += 1
            if position == 0:
                role = ROLE_MANAGER
            elif (position - 1) % group_size == 0:
                role = ROLE_GROUP_LEADER
            else:
                role = ROLE_WORKER
            group = _group_of(role, dept_id, position, group_size)
            rows.append(
                _employee(
                    uid,
                    f"{surname}{given}",
                    dept_id,
                    dept_name,
                    role,
                    "COMPANY-A",
                    group=group,
                    bound_depts=bound_depts if role == ROLE_MANAGER else (),
                )
            )

    secondary_depts = settings.company_b_departments
    per_dept_b = max(settings.headcount_secondary // max(secondary_depts, 1), 1)
    for dept_index in range(secondary_depts):
        dept_id = f"dept-b{dept_index + 1}"
        dept_name = "乙厂车间" if dept_index == 0 else _workshop_name(dept_index)
        for position in range(per_dept_b):
            uid = f"02{dept_index * per_dept_b + position + 1:03d}"
            anchored = _ANCHORED_EMPLOYEES.get(uid)
            if anchored is not None:
                rows.append(dict(anchored))
                continue
            surname = _SURNAMES[name_index % len(_SURNAMES)]
            given = _GIVEN_NAMES[(name_index * 7) % len(_GIVEN_NAMES)]
            name_index += 1
            role = ROLE_MANAGER if position == 0 else ROLE_WORKER
            group = _group_of(role, dept_id, position, group_size)
            rows.append(
                _employee(
                    uid,
                    f"{surname}{given}",
                    dept_id,
                    dept_name,
                    role,
                    "COMPANY-B",
                    group=group,
                )
            )
    return rows


def _group_of(role: str, dept_id: str, position: int, group_size: int) -> str:
    """小组 id for a generated non-manager at ``position`` of ``dept_id``.

    组长 (01) lead every ``group_size``-th member run; workers (00) join the
    same group as their position dictates. 管理 (02) and 老板 (99) are not
    production-group members and carry no group.
    """
    if role in (ROLE_MANAGER, ROLE_BOSS):
        return ""
    return group_id(dept_id, max((position - 1) // group_size, 0) + 1)


def master_rows(settings: MockMesSettings) -> list[RowInsert]:
    """Master tables: depts, employees, styles, processes, menus, hanging.

    Scale comes from the settings (headcount / departments / group size); the
    result is deterministic for the same settings so repeated days and windows
    never drift.
    """
    depts = _dept_rows(settings)
    employees = _employee_rows(settings)

    user_info: list[dict[str, object]] = [
        {
            "code": "Admin",
            "username": "admin",
            "realname": "模拟管理员",
            "companyName": "模拟服装厂A",
        }
    ]
    move_menu: list[dict[str, object]] = [
        {
            "uid": "01001",
            "uname": "模拟员工甲",
            "dept": "dept-a1",
            "menus": [{"name": "扫码产量", "model": "scan", "isScan": True, "sort": 1}],
        }
    ]

    # Styles: the two anchored ones keep their original payloads; the rest fill
    # out a realistic factory catalogue (a 500-person plant runs dozens).
    huohao: list[dict[str, object]] = [
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
    for index in range(3, max(settings.styles, 2) + 1):
        bh = f"HH{index:03d}"
        huohao.append(
            {
                "bh": bh,
                "bbreed": f"模拟款{_STYLE_SUFFIX[index % len(_STYLE_SUFFIX)]}",
                "name_pk": f"MNK{index:02d}",
                "description": f"模拟品名{_STYLE_SUFFIX[index % len(_STYLE_SUFFIX)]}",
                "stype": "T1",
                "huohaotype": _STYLE_KINDS[index % len(_STYLE_KINDS)],
                "dw": "件",
                "lpinpai": "模拟品牌",
                "isdelete": False,
                "jst_huohao": f"JST-{bh}",
            }
        )
    sc_types: list[dict[str, object]] = [
        {
            "bh": "SC1",
            "name": "大身",
            "name_pk": "DS",
            "sfjcj": True,
            "isdelete": False,
            "sfcprk": False,
        },
    ]
    #: 工序（worktype）= 做什么活；车种（vehicle_type）= 用什么设备做。
    #: 工序名采用客户文档真实示例（AI问答对外接口.md：裁剪/验布等）；
    #: 车种按工序差异化（客户示例：裁剪工序的 vehicle_type 为「平车」）。
    rfid_worktypes: list[dict[str, object]] = [
        {
            "bh": f"WT{index:02d}",
            "name": name,
            "name_pk": name_pk,
            "gxtype": 0,
            "isdelete": False,
            "section": section,
            "jc": name[:1],
            "sc_type": f"SYS-WT{index:02d}",
            "worktype_group": worktype_group,
            "yfgs": 2,
            "default_price": price,
            "gongzi_js_type": 1,
            "wt_sort": index * 10,
            "xz_price": str(_d(price) * 2),
            "default_working_hours": "0.50",
            "vehicle_type": vehicle_type,
        }
        for index, (name, name_pk, price, section, worktype_group, vehicle_type) in enumerate(
            [
                ("裁剪", "CJ", "1.2500", "前道", "前道组", "平车"),
                ("钉扣", "DK", "0.8000", "缝制", "缝制组", "手工"),
                ("验布", "YB", "1.0000", "后道", "后道组", "验布机"),
            ],
            start=1,
        )
    ]
    huohao_worktypes: list[dict[str, object]] = [
        {
            "id": f"hw-{style['bh']}-{wt}",
            "huohao": style["bh"],
            "huohaoname": str(style["bbreed"]),
            "wt": wt,
            "wtname": wt_name,
            "sort": sort,
            "sctype": "SC1",
            "sctypename": "大身",
            "sfzb": 0,
            "using_state": 1,
            "zhgx": 1 if sort == 3 else 0,
            "sfxs": 1,
            "theoretical_work_hours": "0.50",
        }
        for style in huohao
        for sort, (wt, wt_name) in enumerate(
            [("WT01", "裁剪"), ("WT02", "钉扣"), ("WT03", "验布")], start=1
        )
    ]

    dg: list[dict[str, object]] = [
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
    dg_zu: list[dict[str, object]] = [
        {
            "id": "dgz1",
            "dgname": "一号吊挂线",
            "xianhao": "1",
            "zuBieName": "一组",
            "company": "COMPANY-A",
        }
    ]

    rows = [RowInsert("mock_dept", payload=row, company=str(row["company"])) for row in depts]
    rows += [
        RowInsert("mock_employee", payload=row, id=str(row["uid"]), company=str(row["company"]))
        for row in employees
    ]
    rows += [RowInsert("mock_huohao", payload=row, id=str(row["bh"])) for row in huohao]
    rows += [RowInsert("mock_sc_type", payload=row, id=str(row["bh"])) for row in sc_types]
    rows += [
        RowInsert("mock_rfid_worktype", payload=row, id=str(row["bh"])) for row in rfid_worktypes
    ]
    rows += [
        RowInsert("mock_huohao_worktype", payload=row, id=str(row["id"]))
        for row in huohao_worktypes
    ]
    rows += [RowInsert("mock_user_info", payload=row, id=str(row["code"])) for row in user_info]
    rows += [
        RowInsert("mock_move_menu", payload=row, id=str(row["uid"]), company="COMPANY-A")
        for row in move_menu
    ]
    rows += [
        RowInsert("mock_dg", payload=row, id=str(row["id"]), company="COMPANY-A") for row in dg
    ]
    rows += [
        RowInsert("mock_dg_zu", payload=row, id=str(row["id"]), company="COMPANY-A")
        for row in dg_zu
    ]
    return rows


# ---------------------------------------------------------------------------
# Anchored business rows (fixed to their dates).
# ---------------------------------------------------------------------------

_WTNAME = {"WT01": "裁剪", "WT02": "钉扣", "WT03": "验布"}

_PLANS: list[dict[str, object]] = [
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

#: (detail id, plan dh, uid, uname, dept, worktype, qty, price, day)
_SCAN_PLAN: list[tuple[str, str, str, str, str, str, str, str, str]] = [
    ("1001", "ZD-2607-001", "01001", "模拟员工甲", "dept-a1", "WT01", "4", "1.2500", "2026-07-31"),
    ("1001", "ZD-2608-001", "01001", "模拟员工甲", "dept-a1", "WT01", "5", "1.2500", "2026-08-05"),
    ("1001", "ZD-2608-001", "01001", "模拟员工甲", "dept-a1", "WT03", "4", "1.0000", "2026-08-06"),
    ("1002", "ZD-2608-001", "01002", "模拟员工甲", "dept-a2", "WT01", "3", "1.2500", "2026-08-20"),
    ("1003", "ZD-B-001", "02001", "乙厂员工", "dept-b1", "WT01", "6", "1.2500", "2026-08-20"),
]

#: Cumulative scanned quantity per anchored detail (progress context table).
_SCAN_SSSL: dict[str, str] = {"1001": "13", "1002": "3", "1003": "6"}

#: Anchored sclzd rows keyed by plan dh.
_SCLZD: dict[str, dict[str, object]] = {
    "ZD-2607-001": {
        "id": "1001",
        "chuanghao": "床号1",
        "cjr": "模拟裁剪员",
        "fhsl": "98",
        "sssl": "13",
        "huohao": "HH001",
        "huohaoname": "模拟款A",
        "description": "模拟品名A",
        "color": "黑色",
        "chima": "M",
    },
    "ZD-2608-001": {
        "id": "1002",
        "chuanghao": "床号1",
        "cjr": "模拟裁剪员",
        "fhsl": "49",
        "sssl": "3",
        "huohao": "HH001",
        "huohaoname": "模拟款A",
        "description": "模拟品名A",
        "color": "白色",
        "chima": "L",
    },
    "ZD-B-001": {
        "id": "1003",
        "chuanghao": "床号1",
        "cjr": "模拟裁剪员",
        "fhsl": "30",
        "sssl": "6",
        "huohao": "HH002",
        "huohaoname": "模拟款B",
        "description": "模拟品名B",
        "color": "红色",
        "chima": "M",
    },
}

#: Sclzd dh per anchored plan id (kept identical to the barcode dh references).
_SCLZD_DH: dict[str, str] = {
    "plan-guid-1": "ZD-2607-001",
    "plan-guid-2": "ZD-2608-001",
    "plan-guid-b1": "ZD-B-001",
}


def _anchored_plan(index: int, plan_id: str) -> list[RowInsert]:
    plan = dict(_PLANS[index])
    plan["id"] = plan_id
    rows = [RowInsert("mock_plan", payload=plan, company=str(plan["company"]))]

    dh = _SCLZD_DH.get(plan_id)
    if int(str(plan["sl"])) > 0 and dh is not None:
        detail = dict(_SCLZD[dh])
        sclzd: dict[str, object] = {
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
            "chuanghao": detail["chuanghao"],
            "cjr": detail["cjr"],
            "zdr": "admin",
            "state": 1,
            "id": detail["id"],
            "baohao": "包1",
            "ganghao": "缸1",
            "color": str(plan["color"]),
            "chima": str(plan["chima"]),
            "fhsl": detail["fhsl"],
            "sssl": detail["sssl"],
            "remark": "",
            "company": str(plan["company"]),
            "dept": str(plan["dept"]),
        }
        rows.append(RowInsert("mock_sclzd", payload=sclzd, company=str(plan["company"])))
        for sort, (wt, wt_name) in enumerate(
            [("WT01", "裁剪"), ("WT02", "钉扣"), ("WT03", "验布")], start=1
        ):
            rows.append(
                RowInsert(
                    "mock_sclzd_worktype",
                    payload={
                        "id": f"sw-{detail['id']}-{wt}",
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
                        "company": str(plan["company"]),
                        "dept": str(plan["dept"]),
                    },
                    company=str(plan["company"]),
                )
            )
    return rows


def _anchored_scan(index: int) -> list[RowInsert]:
    did, dh, uid, uname, dept, wt, qty, price, day = _SCAN_PLAN[index]
    company = "COMPANY-B" if dept == "dept-b1" else "COMPANY-A"
    je = _d(qty) * _d(price)
    base: dict[str, object] = {
        "inputtime": f"{day} 09:30:00",
        "uid": uid,
        "uname": uname,
        "dept": dept,
        "deptname": _DEPT_NAME[dept],
        "rq": day,
        "chuanghao": "床号1",
        "sctype": "SC1",
        "sctypename": "大身",
        "baohao": "包1",
        "id": did,
        "huohao": "HH001" if company == "COMPANY-A" else "HH002",
        "bbreed": "模拟款A" if company == "COMPANY-A" else "模拟款B",
        "description": "模拟品名A" if company == "COMPANY-A" else "模拟品名B",
        "color": "黑色" if company == "COMPANY-A" else "红色",
        "chima": "M",
        "worktype": wt,
        "wtname": _WTNAME[wt],
        "fhsl": _SCLZD[dh]["fhsl"],
        "price": price,
        "company": company,
    }
    rows = [
        RowInsert(
            "mock_barcode_cl",
            payload={**base, "sssl": qty, "sl": _SCLZD[dh]["fhsl"], "je": str(je)},
            company=company,
        ),
        RowInsert(
            "mock_ysk",
            payload={
                **base,
                "inputtime_raw": f"{day} 09:30:00.000",
                "sl": qty,
                "je": str(je),
                "cid": f"cid-{did}-{wt}",
                "sffb": 0,
                "fbid": "",
            },
            company=company,
        ),
        RowInsert(
            "mock_barcode",
            payload={
                "dh": dh,
                "detailId": did,
                "uid": uid,
                "uname": uname,
                "dept": dept,
                "worktype": wt,
                "inputtime": f"{day} 09:30:00",
                "company": company,
            },
            company=company,
        ),
    ]
    if wt == "WT03":
        rows.append(
            RowInsert(
                "mock_dg_cl",
                payload={
                    "id": f"dgc-{did}-{uid}",
                    "rq": day,
                    "dddh": "JH-2608-001",
                    "chuanghao": "床号1",
                    "huohao": "HH001",
                    "bbreed": "模拟款A",
                    "color": "黑色",
                    "chima": "M",
                    "worktype": wt,
                    "wtname": "验布",
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
                },
                company=company,
            )
        )
    return rows


def _anchored_manual() -> list[RowInsert]:
    """手工账：唯一含次品 cp 的来源（C.5）；je = sl × price 恒等。"""
    je = _d("3") * _d("0.8000")
    pin_feng: dict[str, object] = {
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
        "wtname": "钉扣",
        "dw": "件",
        "js": "3",
        "sl": "3",
        "cp": "1",
        "chuanghao": "床号1",
        "color": "黑色",
        "chima": "M",
        "price": "0.8000",
        "je": str(je),
        "remark": "",
        "company": "COMPANY-A",
    }
    wsk: dict[str, object] = {
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
    return [
        RowInsert("mock_pin_feng", payload=pin_feng, company="COMPANY-A"),
        RowInsert("mock_wsk", payload=wsk, company="COMPANY-A"),
    ]


_DEPT_NAME: dict[str, str] = {
    "dept-a1": "一车间",
    "dept-a2": "二车间",
    "dept-b1": "乙厂车间",
}


def anchored_rows(day: date) -> list[RowInsert]:
    """Rows whose generation day is ``day``; empty for non-anchor dates."""
    plan: list[RowInsert] = []
    if day == ANCHOR_PLAN_1:
        plan += _anchored_plan(0, "plan-guid-1")
    elif day == ANCHOR_PLAN_2:
        plan += _anchored_plan(1, "plan-guid-2")
        plan += _anchored_plan(3, "plan-guid-b1")
    elif day == ANCHOR_PLAN_3:
        plan += _anchored_plan(2, "plan-guid-3")

    if day == ANCHOR_SCAN_1:
        plan += _anchored_scan(0)
    elif day == ANCHOR_SCAN_2:
        plan += _anchored_scan(1)
    elif day == ANCHOR_SCAN_3:
        plan += _anchored_scan(2)
        plan += _anchored_manual()
    elif day == ANCHOR_SCAN_4:
        plan += _anchored_scan(3)
        plan += _anchored_scan(4)
    return plan


__all__ = [
    "ANCHOR_PLAN_1",
    "ANCHOR_PLAN_2",
    "ANCHOR_PLAN_3",
    "ANCHOR_SCAN_1",
    "ANCHOR_SCAN_2",
    "ANCHOR_SCAN_3",
    "ANCHOR_SCAN_4",
    "DayPlan",
    "HOLIDAYS",
    "RowInsert",
    "anchored_rows",
    "master_rows",
]
