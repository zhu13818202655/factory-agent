"""Shared fixtures for repository tests."""

import logging

# ---------------------------------------------------------------------------
# litellm 在 import 时默认把仓库根 ``.env`` 注入 ``os.environ``，会把真实环境
# 配置（如 FACTORY_AGENT_CANONICAL_MES_BASE_URL=客户 MES 根地址）泄漏进单测：
# FactoryAgentSettings 因此装配 token 网关，凡走 tenant/user 降级头的 API 测试
# 一律 401。这里在收集任何测试模块之前主动触发一次注入并立即清除
# FACTORY_AGENT_*，保证测试内的 settings 构造只看到测试本意提供的环境。
# 例外：FACTORY_AGENT_TEST_POSTGRES_URL 是集成套件显式注入的测试库 DSN
# （tests/integration/test_session_store_postgres.py），不来自 .env，需保留。
# 同理保留 FACTORY_AGENT_TEST_S3_* ：S3 集成套件（tests/integration/test_artifact_store_s3.py）
# 显式指向本机 SeaweedFS；漏列会让该套件永远拿不到配置而静默 skip。
# ---------------------------------------------------------------------------
import os as _os
from collections.abc import Iterator
from typing import Any

import pytest

try:
    # 只为副作用导入：litellm 在 import 时把仓库根 .env 注入 os.environ。
    # 用 importlib 显式表达「触发导入」，避免留一个没人读取的 import 绑定
    # （ruff 与 pyright 都会把它报成未使用导入，再靠抑制注释盖掉）。
    from importlib import import_module as _import_module

    _import_module("litellm")
except Exception:  # pragma: no cover - 环境缺 litellm 时无需清理
    pass
_KEEP = frozenset(
    {
        "FACTORY_AGENT_TEST_POSTGRES_URL",
        "FACTORY_AGENT_TEST_S3_ENDPOINT_URL",
        "FACTORY_AGENT_TEST_S3_BUCKET",
        "FACTORY_AGENT_TEST_S3_ACCESS_KEY",
        "FACTORY_AGENT_TEST_S3_SECRET_KEY",
    }
)
for _k in list(_os.environ):
    if _k.startswith("FACTORY_AGENT_") and _k not in _KEEP:
        del _os.environ[_k]


@pytest.fixture(autouse=True)
def _loguru_forward_to_logging() -> Iterator[None]:  # pyright: ignore[reportUnusedFunction]
    """Bridge Loguru records back into stdlib ``logging`` so ``caplog`` works.

    The application logs through Loguru (ADR-0004); tests assert on log
    content via ``caplog``. This fixture forwards every Loguru record to the
    stdlib logger named after its ``component``, then resets Loguru handlers
    so a previous test's ``configure_logging`` sink cannot leak into the next.
    """
    from loguru import logger as loguru_logger

    # A prior test may have installed the logging→Loguru bridge via
    # ``configure_logging``; remove it so forwarded records do not loop back.
    logging.root.handlers = [
        handler
        for handler in logging.root.handlers
        if type(handler).__module__ != "factory_agent.observability.logging_adapter"
    ]

    def _forward(message: Any) -> None:
        record = message.record
        name = record["extra"].get("component", "app")
        std_logger = logging.getLogger(name)
        std_logger.handle(
            logging.LogRecord(
                name=name,
                level=record["level"].no,
                pathname=record["file"].path,
                lineno=record["line"],
                msg=record["message"],
                args=(),
                exc_info=record["exception"],
                func=record["function"],
            )
        )

    loguru_logger.remove()
    loguru_logger.add(_forward, level=0)
    yield
