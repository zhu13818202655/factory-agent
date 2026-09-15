"""Test bootstrap: expose the usage-admin test support package on sys.path."""

import os
import sys
from pathlib import Path

# ---------------------------------------------------------------------------
# 与父仓 factory-agent 的 ``tests/conftest.py`` 同一口径：进程环境里可能带着仓库根
# ``.env``（VS Code 的 launch.json 用 ``envFile`` 注入，且 pytest 的启动配置共用同
# 一个文件），而 ``UsageAdminSettings`` 会读 ``USAGE_ADMIN_*``。泄漏进来会让「无参
# 构造 = 内存后端」这类断言失真（实测退化成 S3ExportFileStore），API 用例还会静默
# 把对象写进本机 SeaweedFS 的真实桶。这里在收集任何测试模块之前先清掉。
# 例外：USAGE_ADMIN_TEST_DATABASE_URL 是集成套件
# （tests/integration/test_postgres_store.py）显式注入的测试库 DSN，不来自 .env。
# ---------------------------------------------------------------------------
_KEEP = frozenset({"USAGE_ADMIN_TEST_DATABASE_URL"})
for _key in list(os.environ):
    if _key.startswith("USAGE_ADMIN_") and _key not in _KEEP:
        del os.environ[_key]

_ROOT = Path(__file__).resolve().parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))
