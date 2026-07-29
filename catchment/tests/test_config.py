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


def test_langfuse_accepts_its_own_variable_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Langfuse's docs name these without our prefix; both must work.

    Missing tracing only warns, so a silently-dropped key is easy to miss.
    """
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-unprefixed")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-unprefixed")
    monkeypatch.setenv("LANGFUSE_BASE_URL", "http://langfuse:3000")

    settings = Settings()

    public, secret = settings.require_langfuse()
    assert public.get_secret_value() == "pk-lf-unprefixed"
    assert secret.get_secret_value() == "sk-lf-unprefixed"
    assert settings.langfuse_host == "http://langfuse:3000"


def test_prefixed_langfuse_name_wins_over_the_bare_one(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-bare")
    monkeypatch.setenv("CATCHMENT_LANGFUSE_PUBLIC_KEY", "pk-prefixed")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-bare")

    public, _ = Settings().require_langfuse()

    assert public.get_secret_value() == "pk-prefixed"


@pytest.mark.parametrize(
    ("variable", "attribute"),
    [
        ("CATCHMENT_LLM_MODEL", "llm_model"),
        ("CATCHMENT_LLM_PROVIDER", "llm_provider"),
        ("CATCHMENT_EMBEDDER_URL", "embedder_url"),
        ("CATCHMENT_EMBEDDING_MODEL", "embedding_model"),
    ],
)
def test_blank_env_var_falls_back_to_the_default(
    monkeypatch: pytest.MonkeyPatch, variable: str, attribute: str
) -> None:
    """`CATCHMENT_LLM_MODEL=` is what copying .env.example leaves behind.

    Read literally it shadows the default and the request goes out with an
    empty model, failing at the provider — far from the real cause.
    """
    monkeypatch.setenv(variable, "")

    value = getattr(Settings(), attribute)

    assert value, f"{attribute} fell back to an empty value"
    assert value == Settings.model_fields[attribute].default


def test_whitespace_only_env_var_also_falls_back(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CATCHMENT_LLM_MODEL", "   ")
    assert Settings().llm_model == "openai/gpt-oss-120b"
