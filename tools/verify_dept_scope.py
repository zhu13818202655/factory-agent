"""真实 MES 部门范围核对（直连客户 MES）。

用法:
    set -a; source .env; set +a
    .venv/bin/python tools/verify_dept_scope.py

对 00/01/02/99 四个角色各跑一遍生产链路：token 交换 → 授权 → 部门树展开 →
EmployeeQuery 取数，核对本地 ``DataScope.dept_ids`` 与 MES 实际返回的员工部门
集合：

- 00 员工：MES 只返回本人 → 本地不以部门设界（dept_ids 为空）。
- 01 组长：MES 返回本人所属部门 → 本地 = 本人部门 + 全部子部门。
- 02 管理：MES 返回绑定部门 → 本地 = 绑定部门 + 全部子部门。
- 99 老板：MES 返回全厂 → 本地不设上限（dept_ids 为空）。

核心不变量：MES 返回的部门集合必须被本地部门范围覆盖，否则本地会误拒用户提出
的合法部门过滤。任一角色核对失败则以非零码退出。凭据值不打印。
"""

import asyncio
import os
import sys
from datetime import datetime, timezone

from factory_agent.application.authorization import (
    AuthorizationService,
    FixedScopeVersionAssigner,
)
from factory_agent.data_api.catalog import load_catalog
from factory_agent.data_api.credentials import MesCredentialBundle
from factory_agent.data_api.directory import MesDirectorySource
from factory_agent.data_api.hongzhao import HongzhaoMesAdapter
from factory_agent.data_api.token_gateway import (
    GatewayTokenRefresher,
    TokenBackedMembershipResolver,
    TokenCredentialExchange,
)
from factory_agent.domain import NarrowedFilters, Role, TenantId
from factory_agent.ports.contracts import TrustedCredential

ROLES = ("00", "01", "02", "99")
ROLE_LABEL = {"00": "员工", "01": "组长", "02": "管理", "99": "老板"}
WIDE_RANGE = (
    datetime(2000, 1, 1, tzinfo=timezone.utc),
    datetime(2100, 1, 1, tzinfo=timezone.utc),
)
PAGE_SIZE = 5000
#: Readiness placeholder; the per-caller bundle is bound by the gateway.
PLACEHOLDER = MesCredentialBundle(  # nosec B106 - placeholder, no real secret
    access_token="unconfigured",
    app_key="unconfigured",
    sign="unconfigured",
    timestamp=0,
    expires_at=datetime.max.replace(tzinfo=timezone.utc),
    user="unconfigured",
    uname="unconfigured",
)


async def check(role: str, base_url: str) -> bool:
    credential_value = os.environ.get(f"MES_USER_CREDENTIAL_{role}")
    if not credential_value:
        print(f"[{role} {ROLE_LABEL[role]}] 跳过：未配置 MES_USER_CREDENTIAL_{role}")
        return True

    catalog = load_catalog()
    gateway = TokenCredentialExchange(base_url)
    adapter = HongzhaoMesAdapter(
        base_url, PLACEHOLDER, catalog, refresher=GatewayTokenRefresher(gateway)
    )
    directory = MesDirectorySource(adapter, catalog)
    service = AuthorizationService(
        TokenBackedMembershipResolver(gateway), directory, FixedScopeVersionAssigner()
    )

    principal = await gateway.authenticate(credential_value)
    credential = TrustedCredential(
        tenant_id=principal.credential.tenant_id, user_id=principal.credential.user_id
    )
    with gateway.bind_for(credential):
        resolved = await service.authorize(credential, datetime.now(timezone.utc))
        scope = resolved.data_scope
        rows = await adapter.fetch_resource_rows(
            "EmployeeQuery",
            NarrowedFilters(tenant_id=scope.tenant_id, employee_ids=None, dept_ids=None),
            WIDE_RANGE,
            PAGE_SIZE,
        )
        tree = await directory.list_dept_records(scope.tenant_id)
    await gateway.aclose()

    mes_depts = sorted({str(row.get("dept")) for row in rows if row.get("dept")})
    local_depts = sorted(str(item) for item in scope.dept_ids)
    role_name = ROLE_LABEL[role]
    print(f"\n[{role} {role_name}] user={principal.credential.user_id} 姓名={principal.display_name}")
    print(f"  角色代码     : {principal.role.mes_code} ({principal.role.value})")
    print(f"  本地部门范围 : {local_depts or '（不设界）'}")
    print(f"  MES 可见部门 : {mes_depts}  员工 {len(rows)} 人")
    print(f"  部门字典     : {len(tree)} 个节点，父链 " + ", ".join(
        f"{record.dept_id}<-{record.parent_id or '根'}" for record in tree
    ))

    if principal.role is Role.OWNER:
        ok = not local_depts
        detail = "老板不设部门上限，范围由 MES 全量返回"
    elif principal.role is Role.EMPLOYEE:
        ok = not local_depts and len(rows) <= 1
        detail = "员工仅本人，部门不设界"
    else:
        uncovered = sorted(set(mes_depts) - set(local_depts))
        ok = not uncovered
        detail = "本地范围覆盖 MES 返回部门" if ok else f"本地范围漏掉 {uncovered}"
    print(f"  结论         : {'通过' if ok else '失败'} —— {detail}")
    return ok


async def main() -> int:
    base_url = os.environ.get("FACTORY_AGENT_CANONICAL_MES_BASE_URL")
    if not base_url:
        print("缺少 FACTORY_AGENT_CANONICAL_MES_BASE_URL（先 set -a; source .env; set +a）")
        return 2
    print(f"目标 MES: {base_url}")
    results = [await check(role, base_url) for role in ROLES]
    print("\n汇总:", "全部通过" if all(results) else "存在失败角色")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
