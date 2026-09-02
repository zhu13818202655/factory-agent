"""AppKey masking unit tests (D9)."""

from __future__ import annotations

from usage_admin.masking import mask_app_key


def test_long_key_is_masked_to_first_six_plus_stars() -> None:
    assert mask_app_key("fac-0123456789") == "fac-01***"


def test_exactly_six_chars_is_fully_masked() -> None:
    assert mask_app_key("abcdef") == "***"


def test_short_key_is_fully_masked() -> None:
    assert mask_app_key("abc") == "***"


def test_empty_string_stays_empty() -> None:
    assert mask_app_key("") == ""


def test_none_stays_none() -> None:
    assert mask_app_key(None) is None


def test_non_ascii_key_is_masked_by_code_point() -> None:
    assert mask_app_key("工厂一号厂-key-123") == "工厂一号厂-***"


def test_masking_is_never_identity_for_reasonable_keys() -> None:
    masked = mask_app_key("tenant-abc-123")
    assert masked is not None
    assert "tenant-abc-123" != masked
    assert masked.endswith("***")
