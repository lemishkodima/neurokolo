from __future__ import annotations

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import urlparse

from pydantic import Field, SecretStr, field_validator, model_validator
from pydantic_settings import (
    BaseSettings,
    PydanticBaseSettingsSource,
    SecretsSettingsSource,
    SettingsConfigDict,
)


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    environment: Literal["development", "test", "production"] = "development"
    log_level: str = "INFO"
    database_url: str = "postgresql+asyncpg://club:club@localhost:5432/club"

    bot_token: SecretStr
    bot_username: str
    bot_webhook_secret: SecretStr
    public_base_url: str
    membership_site_url: str
    support_username: str = "support"
    default_plan_code: str = "club"
    admin_telegram_ids: list[int] = Field(default_factory=lambda: [402152266])

    internal_api_key: SecretStr
    wayforpay_merchant_account: str
    wayforpay_merchant_domain: str
    wayforpay_secret_key: SecretStr
    wayforpay_merchant_password: SecretStr
    wayforpay_api_url: str = "https://api.wayforpay.com/regularApi"
    wayforpay_checkout_url: str = "https://secure.wayforpay.com/pay"
    wayforpay_regular_count: int = Field(default=24, ge=1, le=99)

    invite_ttl_seconds: int = Field(default=3600, ge=60, le=86400)
    payment_grace_period_hours: int = Field(default=24, ge=0, le=168)
    payment_grace_reminder_hours_before: int = Field(default=2, ge=1, le=24)
    failed_payment_admin_alerts_enabled: bool = False
    recurring_status_recheck_minutes: int = Field(default=60, ge=5, le=1440)
    worker_interval_seconds: int = Field(default=60, ge=10, le=3600)
    broadcast_batch_size: int = Field(default=25, ge=1, le=100)

    @classmethod
    def settings_customise_sources(
        cls,
        settings_cls: type[BaseSettings],
        init_settings: PydanticBaseSettingsSource,
        env_settings: PydanticBaseSettingsSource,
        dotenv_settings: PydanticBaseSettingsSource,
        file_secret_settings: PydanticBaseSettingsSource,
    ) -> tuple[PydanticBaseSettingsSource, ...]:
        sources = (init_settings, env_settings, dotenv_settings)
        secrets_path = Path("/run/secrets")
        if secrets_path.is_dir():
            return (
                *sources,
                SecretsSettingsSource(settings_cls, secrets_dir=secrets_path),
            )
        return sources

    @field_validator("public_base_url", "membership_site_url", mode="after")
    @classmethod
    def strip_trailing_slash(cls, value: str) -> str:
        return value.rstrip("/")

    @field_validator("bot_username", "support_username", mode="after")
    @classmethod
    def strip_at_sign(cls, value: str) -> str:
        return value.removeprefix("@")

    @model_validator(mode="after")
    def validate_production_configuration(self) -> Settings:
        if self.environment != "production":
            return self
        errors: list[str] = []
        for name, value in (
            ("PUBLIC_BASE_URL", self.public_base_url),
            ("MEMBERSHIP_SITE_URL", self.membership_site_url),
        ):
            parsed = urlparse(value)
            if parsed.scheme != "https" or not parsed.netloc:
                errors.append(f"{name} must be an absolute HTTPS URL")
            if "example." in parsed.netloc.casefold():
                errors.append(f"{name} must not use an example domain")
        if (
            not self.wayforpay_merchant_domain
            or "example." in self.wayforpay_merchant_domain.casefold()
            or "://" in self.wayforpay_merchant_domain
        ):
            errors.append("WAYFORPAY_MERCHANT_DOMAIN must be a real domain without a scheme")
        if "club:club@" in self.database_url:
            errors.append("DATABASE_URL must not use the development database password")
        webhook_secret = self.bot_webhook_secret.get_secret_value()
        if len(webhook_secret) < 32 or not all(
            character.isalnum() or character in "_-" for character in webhook_secret
        ):
            errors.append(
                "BOT_WEBHOOK_SECRET must be at least 32 Telegram-compatible characters"
            )
        if len(self.internal_api_key.get_secret_value()) < 32:
            errors.append("INTERNAL_API_KEY must be at least 32 characters")
        if errors:
            raise ValueError("; ".join(errors))
        return self

    @property
    def telegram_webhook_url(self) -> str:
        return f"{self.public_base_url}/webhooks/telegram"

    @property
    def wayforpay_service_url(self) -> str:
        return f"{self.public_base_url}/webhooks/wayforpay"

    @property
    def landing_base_url(self) -> str:
        parsed = urlparse(self.membership_site_url)
        return f"{parsed.scheme}://{parsed.netloc}"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return Settings()
