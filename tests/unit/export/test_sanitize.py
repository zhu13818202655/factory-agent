"""Filename and spreadsheet-cell safety (Story 6): traversal and formula injection."""

from __future__ import annotations

from factory_agent.export.sanitize import (
    build_export_filename,
    sanitize_filename,
)


def test_sanitize_strips_path_separators_and_traversal() -> None:
    assert sanitize_filename("../../etc/passwd") == "etc_passwd"
    assert sanitize_filename("a/b\\c") == "a_b_c"
    assert "/" not in sanitize_filename("/abs/path")
    assert "\\" not in sanitize_filename("back\\slash")


def test_sanitize_drops_leading_formula_markers() -> None:
    assert sanitize_filename("=SUM(A1)") == "SUM_A1"
    assert sanitize_filename("+cmd") == "cmd"
    assert sanitize_filename("@import") == "import"


def test_sanitize_never_returns_an_empty_or_dot_name() -> None:
    assert sanitize_filename("..") == "export"
    assert sanitize_filename("") == "export"


def test_sanitize_preserves_chinese_and_underscore_basename() -> None:
    assert sanitize_filename("角色_功能_2026-07-01_2026-08-31") == (
        "角色_功能_2026-07-01_2026-08-31"
    )


def test_build_export_filename_composes_role_function_range_timestamp() -> None:
    filename = build_export_filename(
        "员工", "个人工资明细", "2026-07-01_2026-08-31", "202608261200"
    )
    assert filename == "员工_个人工资明细_2026-07-01_2026-08-31_202608261200.xlsx"


def test_build_export_filename_sanitizes_each_segment() -> None:
    filename = build_export_filename("=admin", "a/b", "..", "now")
    assert "/" not in filename
    assert "\\" not in filename
    assert not filename.startswith(".")
    assert filename == "admin_a_b_export_now.xlsx"
