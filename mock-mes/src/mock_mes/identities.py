"""Tenant mapping for the mock MES (identity itself comes from PostgreSQL).

Customer contract (`docs/product/需求及方案整理.md`「用户角色定义」): the four
roles 00 员工 / 01 组长 / 02 管理 / 99 老板 come back from ``/api/system/token``
with ``dept`` (and a manager's bound 车间/部门 list). The mock resolves every
Bearer token against the **generated employee master** (``mock_employee``), so
each generated account — ≥1 boss, one manager per department (one of them bound
across workshops), group leaders and workers — can log in at factory scale.
There is no static identity fixture any more; ``APP_KEY_TO_COMPANY`` only maps
the plaintext AppKey to its tenant.

All values are deterministic development fixtures, never real customer
identities.
"""

from __future__ import annotations

Record = dict[str, object]

#: Plaintext AppKey -> tenant (company). One factory, one AppKey.
APP_KEY_TO_COMPANY = {
    "APPKEY-A": "COMPANY-A",
    "APPKEY-B": "COMPANY-B",
}

#: Role tiers mirrored on ``mock_employee.move_admin_role``; kept here for
#: request handling (99 boss / 02 manager / 01 group leader / 00 worker).
ROLE_BOSS = "99"
ROLE_MANAGER = "02"
ROLE_GROUP_LEADER = "01"
ROLE_WORKER = "00"

__all__ = [
    "APP_KEY_TO_COMPANY",
    "ROLE_BOSS",
    "ROLE_GROUP_LEADER",
    "ROLE_MANAGER",
    "ROLE_WORKER",
    "Record",
]
