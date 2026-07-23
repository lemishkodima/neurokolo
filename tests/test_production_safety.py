from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from club_bot.api import create_app
from club_bot.config import Settings
from club_bot.db import create_engine, create_session_factory
from club_bot.models import Base


def settings_values() -> dict[str, object]:
    return {
        "environment": "test",
        "database_url": "sqlite+aiosqlite:///:memory:",
        "bot_token": "123456:testing",
        "bot_username": "club_bot",
        "bot_webhook_secret": "webhook-secret",
        "public_base_url": "https://api.example.test",
        "membership_site_url": "https://example.test/club",
        "internal_api_key": "internal-secret",
        "wayforpay_merchant_account": "merchant",
        "wayforpay_merchant_domain": "example.test",
        "wayforpay_secret_key": "provider-secret",
        "wayforpay_merchant_password": "provider-password",
    }


def test_production_settings_reject_placeholders_and_development_credentials() -> None:
    values = settings_values()
    values["environment"] = "production"
    values["database_url"] = "postgresql+asyncpg://club:club@db:5432/club"
    with pytest.raises(ValidationError) as error:
        Settings(**values)
    message = str(error.value)
    assert "example domain" in message
    assert "development database password" in message
    assert "BOT_WEBHOOK_SECRET" in message
    assert "INTERNAL_API_KEY" in message


def test_valid_production_settings_are_accepted() -> None:
    values = settings_values()
    values.update(
        {
            "environment": "production",
            "database_url": "postgresql+asyncpg://club:strong-password@db:5432/club",
            "bot_webhook_secret": "a" * 48,
            "internal_api_key": "b" * 48,
            "public_base_url": "https://api.neurokolo.test",
            "membership_site_url": "https://neurokolo.test/club",
            "wayforpay_merchant_domain": "neurokolo.test",
        }
    )
    settings = Settings(**values)
    assert settings.environment == "production"


async def test_health_readiness_and_metrics_endpoints() -> None:
    settings = Settings(**settings_values())
    engine = create_engine(settings.database_url)
    app = create_app(settings)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    app.state.container = SimpleNamespace(
        engine=engine,
        session_factory=create_session_factory(engine),
    )
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
            metrics = await client.get("/metrics")
        assert live.status_code == 200
        assert ready.status_code == 200
        assert metrics.status_code == 200
        assert "neurokolo_http_requests_total" in metrics.text
    finally:
        await engine.dispose()
