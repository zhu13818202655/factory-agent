"""Catalog helpers for tests.

``catalog_with_enabled`` builds a variant of the reviewed default catalog in
which the named operations are re-enabled (``ApiCatalog.with_enabled``).
V2 换代（2026-10-11）后 V1 操作在生产目录里 ``enabled: false``（登记保留、
运行时拒绝），但适配层针对这些操作的遗留行为（GongziMxQuery 的 scheme 形状
选择、NRE 吸收、footer 布尔归一化）仍是受保护的回归面——这些测试用变体目录
继续覆盖，同时默认目录的拒绝路径由 ``test_catalog.py`` / ``test_adapter.py``
单独断言。
"""

from factory_agent.data_api.catalog import ApiCatalog, load_catalog


def catalog_with_enabled(*operation_ids: str) -> ApiCatalog:
    """Return the default catalog with the given operations force-enabled."""
    return load_catalog().with_enabled(*operation_ids)
