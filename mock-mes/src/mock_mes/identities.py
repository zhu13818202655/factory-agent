"""Deterministic test identities (moved out of the removed seed.py).

Covers the four customer role tiers confirmed for row-level filtering (M19):
``99`` boss (whole factory), ``02`` manager and ``01`` group leader (own
department), and ``00`` worker (own rows only). These are development
fixtures, never real customer identities.
"""

from __future__ import annotations

Record = dict[str, object]

#: Deterministic test identities covering the four filter tiers (M19):
#: 99 boss / 02 manager / 01 group leader / 00 worker (own data only).
IDENTITIES: dict[str, Record] = {
    # Factory boss (99): sees every row of the company, all departments.
    "boss-a": {
        "app_key": "APPKEY-A",
        "user": "01009",
        "uname": "模拟厂长",
        "company": "COMPANY-A",
        "dept": "dept-a1",
        "move_admin_role": "99",
    },
    # Workshop manager (02): sees only their workshop's rows.
    "manager-a": {
        "app_key": "APPKEY-A",
        "user": "01008",
        "uname": "模拟车间主任",
        "company": "COMPANY-A",
        "dept": "dept-a1",
        "move_admin_role": "02",
    },
    # Group leader (01): same department scope as the manager. 01012 is a
    # generated group leader of dept-a1 (see fixtures.ROLE_GROUP_LEADER).
    "leader-a": {
        "app_key": "APPKEY-A",
        "user": "01012",
        "uname": "模拟组长",
        "company": "COMPANY-A",
        "dept": "dept-a1",
        "move_admin_role": "01",
    },
    # Own-data-only worker (00).
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

__all__ = ["APP_KEY_TO_COMPANY", "IDENTITIES", "Record"]
