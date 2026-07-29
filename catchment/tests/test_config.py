from __future__ import annotations

import pytest
from pydantic import ValidationError

from catchment.config import (
    TAG_DEPTH_HARD_CEILING,
    MissingConfiguration,
    Settings,
    get_settings,
)


def test_settings_read_from_prefixed_env(settings: Settings) -> None:
    assert settings.env == "test"
    assert "catchment_test" in str(settings.database_url)


def test_missing_required_setting_fails_fast(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("CATCHMENT_DATABASE_URL")
    with pytest.raises(ValidationError):
        Settings()


def test_secrets_are_masked_in_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHMENT_IMAP_PASSWORD", "hunter2-should-never-appear")
    rendered = repr(Settings())
    assert "hunter2-should-never-appear" not in rendered
    assert "**********" in rendered


def test_secret_value_requires_explicit_unwrap(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHMENT_IMAP_HOST", "imap.example.com")
    monkeypatch.setenv("CATCHMENT_IMAP_USERNAME", "me")
    monkeypatch.setenv("CATCHMENT_IMAP_PASSWORD", "swordfish")

    _, _, password = Settings().require_imap()
    assert password.get_secret_value() == "swordfish"
    assert "swordfish" not in str(password)


def test_require_imap_raises_when_unconfigured(settings: Settings) -> None:
    with pytest.raises(MissingConfiguration, match="IMAP"):
        settings.require_imap()


def test_require_langfuse_raises_when_unconfigured(settings: Settings) -> None:
    with pytest.raises(MissingConfiguration, match="Langfuse"):
        settings.require_langfuse()


@pytest.mark.parametrize("depth", ["0", str(TAG_DEPTH_HARD_CEILING + 1)])
def test_tag_depth_bounds_are_enforced(
    monkeypatch: pytest.MonkeyPatch, depth: str
) -> None:
    monkeypatch.setenv("CATCHMENT_MAX_TAG_DEPTH", depth)
    with pytest.raises(ValidationError):
        Settings()


def test_langfuse_host_requires_scheme(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHMENT_LANGFUSE_HOST", "localhost:3000")
    with pytest.raises(ValidationError, match="scheme"):
        Settings()


def test_langfuse_host_trailing_slash_normalized(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CATCHMENT_LANGFUSE_HOST", "https://lf.example.com/")
    assert Settings().langfuse_host == "https://lf.example.com"


def test_settings_are_frozen(settings: Settings) -> None:
    with pytest.raises(ValidationError):
        settings.env = "production"  # type: ignore[misc]


def test_get_settings_is_cached() -> None:
    assert get_settings() is get_settings()
