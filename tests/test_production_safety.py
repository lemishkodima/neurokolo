import re
from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import ValidationError

from club_bot.api import create_app
from club_bot.config import Settings
from club_bot.db import create_engine, create_session_factory
from club_bot.models import Base, LandingTemplate
from club_bot.schemas import CheckoutResponse


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


async def test_public_checkout_posts_signed_fields_and_returns_to_claim_link() -> None:
    settings = Settings(**settings_values())
    checkout = CheckoutResponse(
        checkout_token="checkout_token_abcdefghijklmnopqrstuvwxyz",
        order_reference="CLUB-20260723-reference",
        bot_claim_url="https://t.me/club_bot?start=claim_checkout_token_abcdefghijklmnopqrstuvwxyz",
        gateway_url="https://secure.example.test/pay",
        gateway_fields={
            "merchantAccount": "merchant",
            "merchantSignature": "signature",
            "amount": "990.00",
            "currency": "UAH",
            "productName": ["Club access"],
            "productCount": [1],
            "productPrice": ["990.00"],
            "returnUrl": "https://example.test/club",
        },
        expires_at=datetime.now(UTC),
    )
    create_checkout = AsyncMock(return_value=checkout)
    app = create_app(settings)
    app.state.container = SimpleNamespace(
        settings=settings,
        subscription_service=SimpleNamespace(create_checkout=create_checkout),
        settings_service=SimpleNamespace(payment_test_mode_active=AsyncMock(return_value=False)),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        payment_page = await client.get("/checkout", params={"referral_code": "friend"})
        completion_page = await client.get(
            "/checkout/complete",
            params={"token": checkout.checkout_token},
        )
        completion_post_page = await client.post(
            "/checkout/complete",
            params={"token": checkout.checkout_token},
        )

    assert payment_page.status_code == 200
    assert 'action="https://secure.example.test/pay"' in payment_page.text
    assert 'id="wayforpay-checkout"' in payment_page.text
    assert 'document.getElementById("wayforpay-checkout").submit();' in payment_page.text
    assert "body { visibility: hidden;" in payment_page.text
    assert "<noscript><style>body { visibility: visible; }</style></noscript>" in payment_page.text
    assert 'name="productName[]"' in payment_page.text
    assert "990.00 UAH" in payment_page.text
    assert (
        "https://api.example.test/checkout/complete"
        f"?token={checkout.checkout_token}" in payment_page.text
    )
    content_security_policy = payment_page.headers["content-security-policy"]
    assert "form-action https://secure.example.test" in content_security_policy
    nonce_match = re.search(r'<script nonce="([^"]+)">', payment_page.text)
    assert nonce_match is not None
    assert f"script-src 'nonce-{nonce_match.group(1)}'" in content_security_policy
    assert "script-src 'unsafe-inline'" not in content_security_policy
    create_checkout.assert_awaited_once_with(
        plan_code=settings.default_plan_code,
        email=None,
        phone=None,
        referral_code="friend",
        return_url=None,
        test_mode=False,
    )

    assert completion_page.status_code == 200
    assert (
        f"https://t.me/{settings.bot_username}?start=claim_{checkout.checkout_token}"
        in completion_page.text
    )
    assert completion_page.headers["cache-control"] == "no-store"
    assert completion_post_page.status_code == 200
    assert (
        f"https://t.me/{settings.bot_username}?start=claim_{checkout.checkout_token}"
        in completion_post_page.text
    )
    assert completion_post_page.headers["cache-control"] == "no-store"


async def test_public_landing_renders_safe_values_and_proxies_bot_avatar() -> None:
    settings = Settings(**settings_values())
    template = LandingTemplate(
        name="Campaign",
        slug="campaign",
        landing_title="<Neurokolo>",
        channel_title="Club",
        landing_description="Daily & useful",
        html_template=(
            "<!doctype html><html><body><h1>{{landing_title}}</h1>"
            '<img src="{{avatar_url}}" alt="{{channel_title}}">'
            "<p>{{landing_description}}</p>"
            '<a href="{{open_url}}">Open</a>'
            '<a href="{{download_url}}">Download</a></body></html>'
        ),
        download_url="https://telegram.org/apps",
        created_by_telegram_id=402152266,
    )

    class FakeBot:
        async def get_me(self) -> SimpleNamespace:
            return SimpleNamespace(id=123)

        async def get_user_profile_photos(
            self, _user_id: int, *, limit: int
        ) -> SimpleNamespace:
            assert limit == 1
            return SimpleNamespace(photos=[[SimpleNamespace(file_id="safe-file-id")]])

        async def download(self, _photo: object, *, destination: object) -> None:
            destination.write(b"jpeg-avatar")  # type: ignore[attr-defined]

    app = create_app(settings)
    app.state.container = SimpleNamespace(
        settings=settings,
        landing_template_service=SimpleNamespace(
            get_by_slug=AsyncMock(return_value=template)
        ),
        bot=FakeBot(),
    )
    transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        landing = await client.get("/join/campaign")
        avatar = await client.get("/landing-assets/bot-avatar")

    assert landing.status_code == 200
    assert "&lt;Neurokolo&gt;" in landing.text
    assert "Daily &amp; useful" in landing.text
    assert "data:image/jpeg;base64,anBlZy1hdmF0YXI=" in landing.text
    assert "https://t.me/club_bot?start=landing_campaign" in landing.text
    assert "script-src 'none'" in landing.headers["content-security-policy"]
    assert avatar.status_code == 200
    assert avatar.content == b"jpeg-avatar"
    assert avatar.headers["content-type"].startswith("image/jpeg")


async def test_public_landing_returns_not_found_for_unknown_slug() -> None:
    settings = Settings(**settings_values())
    app = create_app(settings)
    app.state.container = SimpleNamespace(
        settings=settings,
        landing_template_service=SimpleNamespace(
            get_by_slug=AsyncMock(return_value=None)
        ),
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/join/missing")
    assert response.status_code == 404
