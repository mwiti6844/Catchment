"""Single entry point for all configuration and secrets.

Hard constraint (CLAUDE.md): nothing else in the codebase may read
``os.environ`` for credentials, and no secret value is ever logged or printed.
Secrets are wrapped in :class:`~pydantic.SecretStr`, whose ``repr`` masks the
value, so an accidental ``logger.info(settings)`` cannot leak them.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, PostgresDsn, RedisDsn, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

Environment = Literal["development", "test", "production"]

# Absolute ceiling for recursive walks over the tag graph, independent of
# configuration. No query may exceed this, even if misconfigured.
TAG_DEPTH_HARD_CEILING = 32


class Settings(BaseSettings):
    """Runtime configuration, sourced from environment variables only."""

    model_config = SettingsConfigDict(
        env_prefix="CATCHMENT_",
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        frozen=True,
    )

    # --- Core infrastructure (required; fail fast when absent) ---
    database_url: PostgresDsn
    redis_url: RedisDsn
    env: Environment = "development"
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    # --- Embeddings ---
    embedding_model: str = "BAAI/bge-m3"
    embedding_dim: int = Field(default=1024, ge=1)

    # --- Tag graph ---
    max_tag_depth: int = Field(default=8, ge=1, le=TAG_DEPTH_HARD_CEILING)

    # --- Langfuse (self-hosted) ---
    langfuse_host: str = "http://localhost:3000"
    langfuse_public_key: SecretStr | None = None
    langfuse_secret_key: SecretStr | None = None

    # --- Ingestion connectors ---
    # App secret used to verify the X-Hub-Signature-256 header on webhooks.
    whatsapp_webhook_secret: SecretStr | None = None
    # Echoed back during Meta's GET subscription handshake.
    whatsapp_verify_token: SecretStr | None = None
    x_bookmarks_token: SecretStr | None = None
    imap_host: str | None = None
    imap_port: int = Field(default=993, ge=1, le=65535)
    imap_username: str | None = None
    imap_password: SecretStr | None = None
    imap_folder: str = "INBOX"
    imap_batch_size: int = Field(default=50, ge=1, le=500)

    @field_validator("langfuse_host")
    @classmethod
    def _require_scheme(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("langfuse_host must include an http:// or https:// scheme")
        return value.rstrip("/")

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    def require_langfuse(self) -> tuple[SecretStr, SecretStr]:
        """Return Langfuse credentials, raising if tracing is not configured.

        Every LLM call is traced, so a missing key is a startup error rather
        than something to silently skip.
        """
        if self.langfuse_public_key is None or self.langfuse_secret_key is None:
            raise MissingConfiguration(
                "Langfuse tracing requires CATCHMENT_LANGFUSE_PUBLIC_KEY and "
                "CATCHMENT_LANGFUSE_SECRET_KEY"
            )
        return self.langfuse_public_key, self.langfuse_secret_key

    def require_whatsapp_secret(self) -> SecretStr:
        """Return the app secret, raising if webhook verification is unconfigured.

        An unset secret must never mean "accept everything" — a webhook that
        cannot be verified is refused.
        """
        if self.whatsapp_webhook_secret is None:
            raise MissingConfiguration(
                "WhatsApp webhooks require CATCHMENT_WHATSAPP_WEBHOOK_SECRET"
            )
        return self.whatsapp_webhook_secret

    def require_imap(self) -> tuple[str, str, SecretStr]:
        """Return IMAP credentials, raising if the email connector is unconfigured."""
        if not self.imap_host or not self.imap_username or self.imap_password is None:
            raise MissingConfiguration(
                "The email connector requires CATCHMENT_IMAP_HOST, "
                "CATCHMENT_IMAP_USERNAME and CATCHMENT_IMAP_PASSWORD"
            )
        return self.imap_host, self.imap_username, self.imap_password


class MissingConfiguration(RuntimeError):
    """Raised when a required setting or secret is absent."""


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Return the process-wide settings singleton."""
    return Settings()


def reset_settings_cache() -> None:
    """Clear the settings cache. Intended for tests only."""
    get_settings.cache_clear()
