from datetime import timedelta
from decimal import Decimal
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from aiogram.types import CallbackQuery, Chat, Message
from aiogram.types import User as TelegramUser
from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from test_production_safety import settings_values
from test_subscription_flow import database as database
from test_subscription_flow import services as services

from club_bot.api import create_app
from club_bot.bot.admin_keyboards import plan_actions_keyboard
from club_bot.bot.admin_router import payment_plan_link, save_plan_trial
from club_bot.config import Settings
from club_bot.domain.enums import CheckoutStatus, SubscriptionStatus
from club_bot.domain.rules import as_utc, utc_now
from club_bot.integrations.wayforpay import InvalidWayForPaySignature
from club_bot.models import CheckoutSession, Plan, Subscription
from club_bot.services.admin import CatalogService
from club_bot.services.subscriptions import CheckoutNotFoundError


def callback(client: Any, reference: str, amount: str, *, approved: bool = True) -> dict[str, Any]:
    payload = {
        "merchantAccount": "merchant",
        "orderReference": reference,
        "amount": amount,
        "currency": "UAH",
        "authCode": reference[-12:],
        "cardPan": "42****42",
        "transactionStatus": "Approved" if approved else "Declined",
        "reasonCode": 1100 if approved else 1101,
        "reason": "Ok" if approved else "Declined",
        "processingDate": int(utc_now().timestamp()),
    }
    payload["merchantSignature"] = client._sign(
        [
            payload[key]
            for key in (
                "merchantAccount",
                "orderReference",
                "amount",
                "currency",
                "authCode",
                "cardPan",
                "transactionStatus",
                "reasonCode",
            )
        ]
    )
    return payload


async def prepare(database: Any, services: Any) -> Any:
    _, factory = database
    users, service, _ = services
    await users.upsert_telegram_user(TelegramUser(id=501, is_bot=False, first_name="Buyer"))
    await users.upsert_telegram_user(TelegramUser(id=502, is_bot=False, first_name="Other"))
    async with factory() as session, session.begin():
        session.add(
            Plan(
                code="trial",
                name="Trial",
                price=990,
                currency="UAH",
                billing_months=3,
                trial_price=10,
                trial_months=1,
            )
        )
    return service


@pytest.mark.parametrize("owner", [None, 501])
async def test_trial_initial_and_regular_amounts_are_snapshotted(
    database: Any,
    services: Any,
    owner: int | None,
) -> None:
    service = await prepare(database, services)
    _, factory = database
    _, _, provider = services
    checkout = await service.create_checkout(
        plan_code="trial",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
        telegram_id=owner,
    )
    assert checkout.gateway_fields["amount"] == "10.00"
    assert checkout.gateway_fields["regularAmount"] == "990.00"
    assert checkout.gateway_fields["regularMode"] == "quarterly"
    assert checkout.gateway_fields["dateNext"] == (utc_now() + relativedelta(months=1)).strftime(
        "%d.%m.%Y"
    )
    async with factory() as session, session.begin():
        plan = await session.scalar(select(Plan))
        plan.price, plan.trial_price, plan.billing_months = 1500, 20, 12
    wrong = callback(provider, checkout.order_reference, "990.00")
    await service.process_callback(wrong)
    assert await service.checkout_status_by_token(checkout.checkout_token) == CheckoutStatus.CREATED
    initial = callback(provider, checkout.order_reference, "10.00")
    assert await service.process_callback(initial)
    result = await service.claim_checkout(checkout.checkout_token, 501)
    sub = result.subscription
    assert sub.billing_amount == Decimal("990.00")
    assert sub.billing_months == 3
    first_end = as_utc(sub.current_period_end)
    assert first_end == as_utc(sub.current_period_start) + relativedelta(months=1)
    assert not await service.process_callback(initial)
    renewal = callback(provider, checkout.order_reference + "_WFPREG-001", "10.00")
    await service.process_callback(renewal)
    view = await service.current_for_telegram_user(501)
    assert as_utc(view.current_period_end) == first_end
    renewal = callback(provider, checkout.order_reference + "_WFPREG-001", "990.00")
    assert await service.process_callback(renewal)
    assert not await service.process_callback(renewal)
    view = await service.current_for_telegram_user(501)
    assert as_utc(view.current_period_end) == first_end + relativedelta(months=3)
    await service.claim_checkout(checkout.checkout_token, 501)
    with pytest.raises(CheckoutNotFoundError):
        await service.claim_checkout(checkout.checkout_token, 502)
    canceled = await service.cancel_for_telegram_user(501)
    assert as_utc(canceled.current_period_end) == as_utc(view.current_period_end)
    next_checkout = await service.create_checkout(
        plan_code="trial",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
        telegram_id=501,
    )
    assert next_checkout.gateway_fields["amount"] == "1500.00"
    assert next_checkout.gateway_fields["regularAmount"] == "1500.00"


async def test_anonymous_renewal_before_claim_is_applied_once(database: Any, services: Any) -> None:
    service = await prepare(database, services)
    _, _, provider = services
    checkout = await service.create_checkout(
        plan_code="trial",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
    )
    await service.process_callback(callback(provider, checkout.order_reference, "10.00"))
    await service.process_callback(
        callback(
            provider,
            checkout.order_reference + "_WFPREG-001",
            "990.00",
        )
    )
    result = await service.claim_checkout(checkout.checkout_token, 501)
    end = as_utc(result.subscription.current_period_end)
    async with database[1]() as session:
        stored = await session.scalar(select(CheckoutSession))
        assert end == as_utc(stored.paid_at) + relativedelta(months=4)
    again = await service.claim_checkout(checkout.checkout_token, 501)
    assert as_utc(again.subscription.current_period_end) == end


async def test_claim_before_callback_waits_and_then_activates(database: Any, services: Any) -> None:
    service = await prepare(database, services)
    _, _, provider = services
    checkout = await service.create_checkout(
        plan_code="trial",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
    )
    assert not (await service.claim_checkout(checkout.checkout_token, 501)).paid
    bad = callback(provider, checkout.order_reference, "10.00")
    bad["merchantSignature"] = "invalid"
    with pytest.raises(InvalidWayForPaySignature):
        await service.process_callback(bad)
    await service.process_callback(
        callback(provider, checkout.order_reference, "10.00", approved=False)
    )
    assert await service.current_for_telegram_user(501) is None
    await service.process_callback(callback(provider, checkout.order_reference, "10.00"))
    assert (await service.current_for_telegram_user(501)).status == SubscriptionStatus.ACTIVE.value
    next_checkout = await service.create_checkout(
        plan_code="trial",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
        telegram_id=501,
    )
    assert next_checkout.gateway_fields["amount"] == "990.00"


async def test_paid_claim_remains_valid_after_checkout_expiry(database: Any, services: Any) -> None:
    service = await prepare(database, services)
    checkout = await service.create_checkout(
        plan_code="trial",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
    )
    await service.process_callback(callback(services[2], checkout.order_reference, "10.00"))
    async with database[1]() as session, session.begin():
        row = await session.scalar(select(CheckoutSession))
        row.expires_at = utc_now() - timedelta(days=1)
    assert (await service.claim_checkout(checkout.checkout_token, 501)).paid


async def test_external_checkout_completion_uses_real_paid_state(
    database: Any, services: Any
) -> None:
    service = await prepare(database, services)
    settings = Settings(**settings_values())
    app = create_app(settings)
    app.state.container = SimpleNamespace(
        settings=settings,
        subscription_service=service,
        settings_service=SimpleNamespace(payment_test_mode_active=AsyncMock(return_value=False)),
    )
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="https://test"
    ) as c:
        page = await c.get("/checkout", params={"plan_code": "trial"})
        assert page.status_code == 200
        assert 'name="regularAmount" value="990.00"' in page.text
        async with database[1]() as session:
            row = await session.scalar(select(CheckoutSession))
            assert row.user_id is None
            token, reference = row.public_token, row.order_reference
        pending = await c.get("/checkout/complete", params={"token": token})
        assert "Очікуємо підтвердження" in pending.text
        assert (await c.post("/checkout/meta-purchase", json={"token": token})).json() is None
        await service.process_callback(callback(services[2], reference, "10.00"))
        paid = await c.post("/checkout/complete", params={"token": token})
        assert "Оплату підтверджено" in paid.text
        assert f"start=claim_{token}" in paid.text
        assert "connect.facebook.net" not in paid.text
        assert "allow-same-origin" not in paid.text
        assert "analytics-yes" not in paid.text and "analytics-no" not in paid.text
        assert "analytics-consent" not in paid.text
        assert "document.body.appendChild(frame)" in paid.text
        assert paid.headers["referrer-policy"] == "no-referrer"
        purchase = await c.post("/checkout/meta-purchase", json={"token": token})
        event = purchase.json()
        assert event["value"] == 10.0  # First payment, not the 990 recurring amount.
        assert event["currency"] == "UAH"
        assert set(event) == {"event_id", "value", "currency"}
        assert token not in purchase.text and reference not in purchase.text
        assert purchase.headers["cache-control"] == "no-store"
        assert (await c.post("/checkout/meta-purchase", json={"token": token})).json() is None
        frame = await c.get("/tracking/meta")
        assert "3714470908697594" in frame.text
        assert "sandbox allow-scripts" in frame.headers["content-security-policy"]
        assert "allow-same-origin" not in frame.headers["content-security-policy"]
        assert token not in frame.text
        assert (await c.get("/checkout/complete", params={"token": "x" * 32})).status_code == 404
        async with database[1]() as session:
            assert await session.scalar(select(Subscription)) is None


async def test_meta_ignores_test_checkout_and_unverified_paid_state(
    database: Any,
    services: Any,
) -> None:
    service = await prepare(database, services)
    checkout = await service.create_checkout(
        plan_code="trial",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
    )
    async with database[1]() as session, session.begin():
        row = await session.scalar(select(CheckoutSession))
        row.status = CheckoutStatus.PAID
    assert await service.claim_meta_purchase(checkout.checkout_token) is None
    await service.process_callback(callback(services[2], checkout.order_reference, "10.00"))
    async with database[1]() as session, session.begin():
        row = await session.scalar(select(CheckoutSession))
        row.order_reference = "TEST-example"
    assert await service.claim_meta_purchase(checkout.checkout_token) is None


async def test_admin_trial_validation_disable_and_archive(database: Any, services: Any) -> None:
    await prepare(database, services)
    catalog = CatalogService(database[1], default_plan_code="base")
    plan = (await catalog.list_plans())[0]
    for price in [Decimal("0"), Decimal("-1"), Decimal("NaN"), Decimal("990"), Decimal("0.001")]:
        with pytest.raises(ValueError):
            await catalog.configure_trial(plan.id, price=price)
    await catalog.configure_trial(plan.id, price=Decimal("1"), months=2)
    configured = await catalog.get_plan(plan.id)
    assert configured.trial_price == 1 and configured.trial_months == 2
    await catalog.configure_trial(plan.id, price=None)
    assert (await catalog.get_plan(plan.id)).trial_price is None
    await catalog.archive_plan(plan.id)
    assert await catalog.configure_trial(plan.id, price=Decimal("1")) is None


async def test_admin_trial_and_external_link_controls(database: Any, services: Any) -> None:
    await prepare(database, services)
    catalog = CatalogService(database[1], default_plan_code="base")
    plan = (await catalog.list_plans())[0]
    keyboard = plan_actions_keyboard(plan, can_archive=True)
    actions = [button.callback_data for row in keyboard.inline_keyboard for button in row]
    assert f"adm:plan_trial:{plan.id}" in actions
    assert f"adm:plan_link:{plan.id}" in actions
    message = Message(
        message_id=1,
        date=utc_now(),
        chat=Chat(id=501, type="private"),
        from_user=TelegramUser(id=501, is_bot=False, first_name="Admin"),
        text="5 2",
    )
    admin = SimpleNamespace(is_admin=AsyncMock(return_value=True))
    state = SimpleNamespace(
        get_data=AsyncMock(return_value={"selected_plan_id": str(plan.id)}),
        clear=AsyncMock(),
    )
    event = CallbackQuery(
        id="event",
        from_user=message.from_user,
        chat_instance="test",
        message=message,
        data=f"adm:plan_link:{plan.id}",
    )
    with (
        patch.object(Message, "answer", new_callable=AsyncMock) as answer,
        patch.object(
            CallbackQuery,
            "answer",
            new_callable=AsyncMock,
        ),
    ):
        await save_plan_trial(message, admin, catalog, state)
        assert (await catalog.get_plan(plan.id)).trial_price == Decimal("5")
        await payment_plan_link(
            event,
            admin,
            catalog,
            SimpleNamespace(public_base_url="https://api.example.com"),
        )
        assert "https://api.example.com/checkout?plan_code=trial" in answer.call_args.args[0]
        assert "claim_" not in answer.call_args.args[0]
        answer.reset_mock()
        admin.is_admin.return_value = False
        await payment_plan_link(
            event,
            admin,
            catalog,
            SimpleNamespace(public_base_url="https://api.example.com"),
        )
        answer.assert_not_called()
