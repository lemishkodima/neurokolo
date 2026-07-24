from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import timedelta
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import httpx
import pytest
from aiogram.types import User as TelegramUser
from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from club_bot.db import create_engine, create_session_factory
from club_bot.domain.enums import (
    CheckoutStatus,
    MembershipStatus,
    RecurringStatus,
    ReferralStatus,
    ResourceType,
    SubscriptionStatus,
)
from club_bot.domain.rules import as_utc, utc_now
from club_bot.integrations.wayforpay import InvalidWayForPaySignature, WayForPayClient
from club_bot.models import (
    Base,
    CheckoutSession,
    Payment,
    Plan,
    Referral,
    ResourceMembership,
    Subscription,
    TelegramResource,
    User,
)
from club_bot.services.access import AccessService, ResourceInvite
from club_bot.services.subscription_notifications import SubscriptionNotificationService
from club_bot.services.subscriptions import SubscriptionService
from club_bot.services.users import UserService


@pytest.fixture
async def database(
    tmp_path: Path,
) -> AsyncIterator[tuple[AsyncEngine, async_sessionmaker[AsyncSession]]]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    yield engine, create_session_factory(engine)
    await engine.dispose()


@pytest.fixture
async def services(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
) -> AsyncIterator[tuple[UserService, SubscriptionService, WayForPayClient]]:
    _, session_factory = database

    async def provider_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/regularApi"
        payload = request.read().decode()
        if '"requestType":"STATUS"' in payload:
            return httpx.Response(
                200,
                json={"reasonCode": 4100, "reason": "Ok", "status": "Active"},
            )
        return httpx.Response(200, json={"reasonCode": 4100, "reason": "Ok"})

    http_client = httpx.AsyncClient(transport=httpx.MockTransport(provider_handler))
    wayforpay = WayForPayClient(
        merchant_account="merchant",
        merchant_domain="example.com",
        secret_key="secret",
        merchant_password="password",
        api_url="https://api.example.test/regularApi",
        checkout_url="https://secure.example.test/pay",
        http_client=http_client,
    )
    test_wayforpay = WayForPayClient(
        merchant_account="test_merchant",
        merchant_domain="example.com",
        secret_key="test-secret",
        merchant_password="test-password",
        api_url="https://api.example.test/regularApi",
        checkout_url="https://secure.example.test/pay",
        http_client=http_client,
    )
    yield (
        UserService(session_factory),
        SubscriptionService(
            session_factory,
            wayforpay,
            test_wayforpay=test_wayforpay,
            bot_username="club_bot",
            service_url="https://bot.example.com/webhooks/wayforpay",
            default_return_url="https://example.com/complete",
        ),
        wayforpay,
    )
    await http_client.aclose()


async def test_paid_checkout_claim_and_idempotency(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    services: tuple[UserService, SubscriptionService, WayForPayClient],
) -> None:
    _, session_factory = database
    user_service, subscriptions, wayforpay = services
    async with session_factory() as session, session.begin():
        session.add(Plan(code="base", name="Base", price=990, currency="UAH"))

    checkout = await subscriptions.create_checkout(
        plan_code="base",
        email="member@example.com",
        phone=None,
        referral_code=None,
        return_url=None,
    )
    callback = {
        "merchantAccount": "merchant",
        "orderReference": checkout.order_reference,
        "amount": "990.00",
        "currency": "UAH",
        "authCode": "123456",
        "cardPan": "42****42",
        "transactionStatus": "Approved",
        "reasonCode": 1100,
        "processingDate": 1_700_000_000,
        "recToken": "rec-token",
    }
    callback["merchantSignature"] = wayforpay._sign(
        [
            callback[key]
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
    assert await subscriptions.process_callback(callback) is True
    assert await subscriptions.process_callback(callback) is False

    telegram_user = TelegramUser(id=123, is_bot=False, first_name="Member")
    await user_service.upsert_telegram_user(telegram_user)
    claim = await subscriptions.claim_checkout(checkout.checkout_token, telegram_user.id)
    assert claim.paid is True
    assert claim.subscription is not None
    assert claim.subscription.status == SubscriptionStatus.ACTIVE
    assert await subscriptions.checkout_owner_telegram_id(checkout.order_reference) == 123

    view = await subscriptions.current_for_telegram_user(telegram_user.id)
    assert view is not None
    assert view.plan_name == "Base"

    renewal = dict(callback)
    renewal["processingDate"] = 1_700_000_100
    renewal["authCode"] = "654321"
    renewal["merchantSignature"] = wayforpay._sign(
        [
            renewal[key]
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
    assert await subscriptions.is_initial_checkout_callback(checkout.order_reference) is False
    assert await subscriptions.process_callback(renewal) is True
    renewed_view = await subscriptions.current_for_telegram_user(telegram_user.id)
    assert renewed_view is not None
    assert renewed_view.current_period_end is not None
    assert view.current_period_end is not None
    assert renewed_view.current_period_end > view.current_period_end

    canceled = await subscriptions.cancel_for_telegram_user(telegram_user.id)
    assert canceled.cancel_at_period_end is True
    assert canceled.current_period_end == renewed_view.current_period_end

    async with session_factory() as session:
        stored_checkout = await session.scalar(
            select(CheckoutSession).where(CheckoutSession.public_token == checkout.checkout_token)
        )
        assert stored_checkout is not None
        stored_payment = await session.scalar(
            select(Payment).where(Payment.checkout_session_id == stored_checkout.id)
        )
        assert stored_checkout.status == CheckoutStatus.CLAIMED
        assert stored_payment is not None
        assert stored_payment.subscription_id == claim.subscription.id


async def test_personal_checkout_activates_without_manual_claim(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    services: tuple[UserService, SubscriptionService, WayForPayClient],
) -> None:
    _, session_factory = database
    user_service, subscriptions, wayforpay = services
    async with session_factory() as session, session.begin():
        plan = Plan(
            code="base",
            name="Base",
            price=2490,
            currency="UAH",
            billing_months=3,
        )
        session.add(plan)
        await session.flush()
        plan_id = plan.id
    telegram_user = TelegramUser(id=501, is_bot=False, first_name="Personal")
    await user_service.upsert_telegram_user(telegram_user)

    checkout = await subscriptions.create_checkout(
        plan_code="base",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
        telegram_id=telegram_user.id,
    )
    assert await subscriptions.checkout_owner_telegram_id_by_token(
        checkout.checkout_token
    ) == telegram_user.id
    assert checkout.gateway_fields["regularMode"] == "quarterly"
    async with session_factory() as session, session.begin():
        stored_plan = await session.get(Plan, plan_id)
        assert stored_plan is not None
        stored_plan.billing_months = 1
    callback = {
        "merchantAccount": "merchant",
        "orderReference": checkout.order_reference,
        "amount": "2490.00",
        "currency": "UAH",
        "authCode": "personal",
        "cardPan": "42****42",
        "transactionStatus": "Approved",
        "reasonCode": 1100,
        "processingDate": 1_700_000_000,
        "recToken": "personal-rec-token",
    }
    callback["merchantSignature"] = wayforpay._sign(
        [
            callback[key]
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

    assert await subscriptions.is_initial_checkout_callback(checkout.order_reference) is True
    assert await subscriptions.process_callback(callback) is True
    assert await subscriptions.checkout_owner_telegram_id(checkout.order_reference) == 501
    subscription = await subscriptions.current_for_telegram_user(501)
    assert subscription is not None
    assert subscription.status == SubscriptionStatus.ACTIVE.value
    assert subscription.auto_renew_enabled is False
    assert subscription.provider_recurring_status == RecurringStatus.PENDING.value

    recurring = await subscriptions.verify_recurring_for_order(checkout.order_reference)
    assert recurring is not None
    assert recurring.status == RecurringStatus.ACTIVE
    subscription = await subscriptions.current_for_telegram_user(501)
    assert subscription is not None
    assert subscription.auto_renew_enabled is True
    assert subscription.provider_recurring_status == RecurringStatus.ACTIVE.value

    async with session_factory() as session:
        stored_checkout = await session.scalar(
            select(CheckoutSession).where(CheckoutSession.public_token == checkout.checkout_token)
        )
        assert stored_checkout is not None
        assert stored_checkout.status == CheckoutStatus.CLAIMED
        assert stored_checkout.billing_months == 3
        stored_subscription = await session.scalar(
            select(Subscription).where(
                Subscription.provider_subscription_id == checkout.order_reference
            )
        )
        assert stored_subscription is not None
        assert stored_subscription.billing_months == 3
        assert stored_subscription.current_period_start is not None
        assert stored_subscription.current_period_end == (
            stored_subscription.current_period_start + relativedelta(months=3)
        )

    renewal = dict(callback)
    renewal["processingDate"] = 1_700_000_100
    renewal["authCode"] = "personal-renewal"
    renewal["merchantSignature"] = wayforpay._sign(
        [
            renewal[key]
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
    assert await subscriptions.process_callback(renewal) is True
    async with session_factory() as session:
        renewed_subscription = await session.scalar(
            select(Subscription).where(
                Subscription.provider_subscription_id == checkout.order_reference
            )
        )
        assert renewed_subscription is not None
        assert renewed_subscription.current_period_start is not None
        assert renewed_subscription.current_period_end == (
            renewed_subscription.current_period_start + relativedelta(months=3)
        )


async def test_missing_recurring_rule_keeps_paid_access_but_disables_auto_renew(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    services: tuple[UserService, SubscriptionService, WayForPayClient],
) -> None:
    _, session_factory = database
    _, subscriptions, _ = services
    now = utc_now()
    async with session_factory() as session, session.begin():
        plan = Plan(code="base", name="Base", price=990, currency="UAH")
        user = User(
            telegram_id=777,
            username="member",
            first_name="Member",
            referral_code="MEMBER777",
        )
        session.add_all([plan, user])
        await session.flush()
        session.add(
            Subscription(
                user_id=user.id,
                plan_id=plan.id,
                status=SubscriptionStatus.ACTIVE,
                current_period_start=now,
                current_period_end=now + relativedelta(months=1),
                billing_amount=990,
                billing_currency="UAH",
                billing_months=1,
                provider="wayforpay",
                provider_subscription_id="CLUB-MISSING-RULE",
                provider_recurring_status=RecurringStatus.PENDING.value,
            )
        )

    subscriptions.wayforpay.recurring_status = AsyncMock(
        return_value={"reasonCode": 4102, "reason": "Rule is not found"}
    )
    result = await subscriptions.verify_recurring_for_order("CLUB-MISSING-RULE")

    assert result is not None
    assert result.status == RecurringStatus.MISSING
    view = await subscriptions.current_for_telegram_user(777)
    assert view is not None
    assert view.status == SubscriptionStatus.ACTIVE.value
    assert view.auto_renew_enabled is False
    assert view.provider_recurring_status == RecurringStatus.MISSING.value


async def test_subscription_list_contains_each_current_tariff(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    services: tuple[UserService, SubscriptionService, WayForPayClient],
) -> None:
    _, session_factory = database
    _, subscriptions, _ = services
    now = utc_now()
    async with session_factory() as session, session.begin():
        user = User(
            telegram_id=778,
            username="multi",
            first_name="Multi",
            referral_code="MEMBER778",
        )
        monthly = Plan(
            code="monthly",
            name="Місячний",
            price=610,
            currency="UAH",
            billing_months=1,
        )
        yearly = Plan(
            code="yearly",
            name="Річний",
            price=6100,
            currency="UAH",
            billing_months=12,
        )
        session.add_all([user, monthly, yearly])
        await session.flush()
        session.add_all(
            [
                Subscription(
                    user_id=user.id,
                    plan_id=monthly.id,
                    status=SubscriptionStatus.ACTIVE,
                    current_period_start=now,
                    current_period_end=now + relativedelta(months=1),
                    billing_amount=610,
                    billing_currency="UAH",
                    billing_months=1,
                    provider="wayforpay",
                    provider_subscription_id="CLUB-MONTHLY",
                    provider_recurring_status=RecurringStatus.ACTIVE.value,
                ),
                Subscription(
                    user_id=user.id,
                    plan_id=yearly.id,
                    status=SubscriptionStatus.ACTIVE,
                    current_period_start=now,
                    current_period_end=now + relativedelta(months=12),
                    billing_amount=6100,
                    billing_currency="UAH",
                    billing_months=12,
                    provider="wayforpay",
                    provider_subscription_id="CLUB-YEARLY",
                    provider_recurring_status=RecurringStatus.MISSING.value,
                ),
            ]
        )

    views = await subscriptions.current_subscriptions_for_telegram_user(778)

    assert [view.plan_name for view in views] == ["Річний", "Місячний"]
    assert [view.billing_amount for view in views] == [6100, 610]
    assert [view.billing_months for view in views] == [12, 1]
    assert [view.auto_renew_enabled for view in views] == [False, True]


async def test_approved_callback_with_wrong_payment_terms_does_not_activate(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    services: tuple[UserService, SubscriptionService, WayForPayClient],
) -> None:
    _, session_factory = database
    user_service, subscriptions, wayforpay = services
    async with session_factory() as session, session.begin():
        session.add(Plan(code="base", name="Base", price=990, currency="UAH"))

    checkout = await subscriptions.create_checkout(
        plan_code="base",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
    )
    callback = {
        "merchantAccount": "merchant",
        "orderReference": checkout.order_reference,
        "amount": "1.00",
        "currency": "USD",
        "authCode": "123456",
        "cardPan": "42****42",
        "transactionStatus": "Approved",
        "reasonCode": 1100,
        "processingDate": 1_700_000_000,
    }
    callback["merchantSignature"] = wayforpay._sign(
        [
            callback[key]
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

    assert await subscriptions.process_callback(callback) is True
    telegram_user = TelegramUser(id=456, is_bot=False, first_name="Member")
    await user_service.upsert_telegram_user(telegram_user)
    claim = await subscriptions.claim_checkout(checkout.checkout_token, telegram_user.id)
    assert claim.paid is False
    assert claim.subscription is None

    async with session_factory() as session:
        stored_checkout = await session.scalar(
            select(CheckoutSession).where(CheckoutSession.public_token == checkout.checkout_token)
        )
        payment = await session.scalar(
            select(Payment).where(Payment.order_reference == checkout.order_reference)
        )
        assert stored_checkout is not None
        assert stored_checkout.status == CheckoutStatus.CREATED
        assert payment is not None
        assert payment.failure_reason == "Payment amount or currency mismatch"


async def test_test_checkout_uses_isolated_provider_and_signature(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    services: tuple[UserService, SubscriptionService, WayForPayClient],
) -> None:
    _, session_factory = database
    _, subscriptions, production_wayforpay = services
    async with session_factory() as session, session.begin():
        session.add(Plan(code="base", name="Base", price=990, currency="UAH"))

    checkout = await subscriptions.create_checkout(
        plan_code="base",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
        test_mode=True,
    )
    assert checkout.order_reference.startswith("TEST-")
    assert checkout.gateway_fields["merchantAccount"] == "test_merchant"
    assert checkout.gateway_fields["productName"] == [
        "[TEST] Base — підписка на 1 місяць, автопродовження"
    ]
    assert checkout.gateway_fields["regularCount"] == 24

    callback = {
        "merchantAccount": "test_merchant",
        "orderReference": checkout.order_reference,
        "amount": "990.00",
        "currency": "UAH",
        "authCode": "test-auth",
        "cardPan": "42****42",
        "transactionStatus": "Approved",
        "reasonCode": 1100,
        "processingDate": 1_700_000_000,
    }
    callback["merchantSignature"] = subscriptions.test_wayforpay._sign(
        [
            callback[key]
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
    subscriptions.verify_callback(callback)
    assert await subscriptions.process_callback(callback) is True

    telegram_user = TelegramUser(id=987, is_bot=False, first_name="Test Admin")
    await UserService(session_factory).upsert_telegram_user(telegram_user)
    claim = await subscriptions.claim_checkout(checkout.checkout_token, telegram_user.id)
    assert claim.subscription is not None
    assert claim.subscription.provider == "wayforpay_test"
    suspend = AsyncMock()
    subscriptions.test_wayforpay.suspend_recurring = suspend
    canceled = await subscriptions.cancel_for_telegram_user(telegram_user.id)
    assert canceled.cancel_at_period_end is True
    suspend.assert_not_awaited()

    forged_with_production = dict(callback)
    forged_with_production["merchantAccount"] = production_wayforpay.merchant_account
    forged_with_production["merchantSignature"] = production_wayforpay._sign(
        [
            forged_with_production[key]
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
    with pytest.raises(InvalidWayForPaySignature):
        subscriptions.verify_callback(forged_with_production)


async def test_referral_registration(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    services: tuple[UserService, SubscriptionService, WayForPayClient],
) -> None:
    _, session_factory = database
    user_service, _, _ = services
    referrer = await user_service.upsert_telegram_user(
        TelegramUser(id=1, is_bot=False, first_name="Referrer")
    )
    referred = await user_service.upsert_telegram_user(
        TelegramUser(id=2, is_bot=False, first_name="Friend"),
        referral_code=referrer.referral_code,
    )
    async with session_factory() as session:
        referral = await session.scalar(
            select(Referral).where(Referral.referred_user_id == referred.id)
        )
        assert referral is not None
        assert referral.status == ReferralStatus.REGISTERED


async def test_activation_notification_contains_personal_invite_button() -> None:
    class FakeAccessService:
        async def create_invites(self, telegram_id: int) -> list[ResourceInvite]:
            assert telegram_id == 123
            return [ResourceInvite(name="Клуб", url="https://t.me/+personal")]

    class FakeBot:
        def __init__(self) -> None:
            self.messages: list[tuple[int, str, object]] = []

        async def send_message(self, chat_id: int, text: str, reply_markup: object) -> None:
            self.messages.append((chat_id, text, reply_markup))

    class FakeSettingsService:
        async def get(self, key: str) -> str:
            assert key == "payment_success_text"
            return "✅ <b>Власний текст успішної оплати</b>"

    bot = FakeBot()
    service = SubscriptionNotificationService(  # type: ignore[arg-type]
        bot,
        FakeAccessService(),  # type: ignore[arg-type]
        FakeSettingsService(),  # type: ignore[arg-type]
    )
    assert await service.send_activated(123) is True
    chat_id, text, markup = bot.messages[0]
    assert chat_id == 123
    assert "Власний текст успішної оплати" in text
    assert markup.inline_keyboard[0][0].text == "Доєднатися 💎"  # type: ignore[union-attr]
    assert markup.inline_keyboard[0][0].url == "https://t.me/+personal"  # type: ignore[union-attr]


async def test_failed_renewal_records_dunning_and_success_clears_it(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    services: tuple[UserService, SubscriptionService, WayForPayClient],
) -> None:
    _, session_factory = database
    user_service, subscriptions, wayforpay = services
    async with session_factory() as session, session.begin():
        session.add(Plan(code="dunning", name="Dunning", price=490, currency="UAH"))
    telegram_user = TelegramUser(id=808, is_bot=False, first_name="Dunning")
    await user_service.upsert_telegram_user(telegram_user)
    checkout = await subscriptions.create_checkout(
        plan_code="dunning",
        email=None,
        phone=None,
        referral_code=None,
        return_url=None,
        telegram_id=telegram_user.id,
    )

    callback: dict[str, Any] = {
        "merchantAccount": "merchant",
        "orderReference": checkout.order_reference,
        "amount": "490.00",
        "currency": "UAH",
        "authCode": "initial",
        "cardPan": "42****42",
        "transactionStatus": "Approved",
        "reasonCode": 1100,
        "processingDate": 1_700_000_000,
        "recToken": "dunning-rec-token",
        "repayUrl": "https://secure.wayforpay.com/repay/safe-token",
    }

    def sign(data: dict[str, Any]) -> None:
        data["merchantSignature"] = wayforpay._sign(
            [
                data[key]
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

    sign(callback)
    assert await subscriptions.process_callback(callback) is True
    async with session_factory() as session, session.begin():
        stored = await session.scalar(
            select(Subscription).where(
                Subscription.provider_subscription_id == checkout.order_reference
            )
        )
        assert stored is not None
        stored.provider_repay_url = None

    declined = dict(callback)
    declined.update(
        {
            "authCode": "",
            "transactionStatus": "Declined",
            "reasonCode": 1101,
            "reason": "Not enough funds",
            "processingDate": 1_700_000_100,
        }
    )
    sign(declined)
    assert await subscriptions.process_callback(declined) is True

    async with session_factory() as session:
        stored = await session.scalar(
            select(Subscription).where(
                Subscription.provider_subscription_id == checkout.order_reference
            )
        )
        assert stored is not None
        assert stored.status == SubscriptionStatus.PAST_DUE
        assert stored.payment_failed_at is not None
        assert stored.payment_failure_reason == "Not enough funds"
        assert stored.provider_repay_url == "https://secure.wayforpay.com/repay/safe-token"

    recovered = dict(callback)
    recovered.update(
        {
            "authCode": "recovered",
            "processingDate": 1_700_000_200,
            "repayUrl": "https://evil.example/steal",
        }
    )
    sign(recovered)
    assert await subscriptions.process_callback(recovered) is True

    async with session_factory() as session:
        stored = await session.scalar(
            select(Subscription).where(
                Subscription.provider_subscription_id == checkout.order_reference
            )
        )
        assert stored is not None
        assert stored.status == SubscriptionStatus.ACTIVE
        assert stored.payment_failed_at is None
        assert stored.payment_failure_reason is None
        assert stored.provider_repay_url == "https://secure.wayforpay.com/repay/safe-token"


async def test_complete_subscription_lifecycle(
    database: tuple[AsyncEngine, async_sessionmaker[AsyncSession]],
    services: tuple[UserService, SubscriptionService, WayForPayClient],
) -> None:
    class FakeInvite:
        invite_link = "https://t.me/+personal"

    class FakeBot:
        def __init__(self) -> None:
            self.banned_users: list[int] = []

        async def create_chat_invite_link(self, **kwargs: Any) -> FakeInvite:
            assert kwargs["creates_join_request"] is True
            assert "member_limit" not in kwargs
            return FakeInvite()

        async def revoke_chat_invite_link(self, **kwargs: Any) -> None:
            pass

        async def approve_chat_join_request(self, **kwargs: Any) -> None:
            pass

        async def decline_chat_join_request(self, **kwargs: Any) -> None:
            pass

        async def ban_chat_member(self, *, chat_id: int, user_id: int) -> None:
            self.banned_users.append(user_id)

        async def unban_chat_member(self, **kwargs: Any) -> None:
            pass

    _, session_factory = database
    user_service, subscriptions, wayforpay = services
    async with session_factory() as session, session.begin():
        resource = TelegramResource(
            code="community",
            name="Community",
            chat_id=-100123,
            resource_type=ResourceType.SUPERGROUP,
        )
        session.add(
            Plan(
                code="lifecycle",
                name="Lifecycle",
                price=990,
                currency="UAH",
                resources=[resource],
            )
        )

    checkout = await subscriptions.create_checkout(
        plan_code="lifecycle",
        email="member@example.com",
        phone=None,
        referral_code=None,
        return_url=None,
    )
    callback = {
        "merchantAccount": "merchant",
        "orderReference": checkout.order_reference,
        "amount": "990.00",
        "currency": "UAH",
        "authCode": "initial",
        "cardPan": "42****42",
        "transactionStatus": "Approved",
        "reasonCode": 1100,
        "processingDate": 1_700_000_000,
        "recToken": "rec-token",
    }
    callback["merchantSignature"] = wayforpay._sign(
        [
            callback[key]
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
    assert await subscriptions.process_callback(callback) is True

    telegram_user = TelegramUser(id=777, is_bot=False, first_name="Lifecycle")
    await user_service.upsert_telegram_user(telegram_user)
    claim = await subscriptions.claim_checkout(checkout.checkout_token, telegram_user.id)
    assert claim.paid is True
    assert claim.subscription is not None

    bot: Any = FakeBot()
    access = AccessService(
        session_factory,
        bot,
        invite_ttl_seconds=3600,
        grace_period_hours=0,
    )
    invites = await access.create_invites(telegram_user.id)
    assert [invite.url for invite in invites] == ["https://t.me/+personal"]

    renewal = dict(callback)
    renewal["authCode"] = "renewal"
    renewal["processingDate"] = 1_700_000_100
    renewal["merchantSignature"] = wayforpay._sign(
        [
            renewal[key]
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
    original_end = claim.subscription.current_period_end
    assert await subscriptions.process_callback(renewal) is True
    renewed = await subscriptions.current_for_telegram_user(telegram_user.id)
    assert renewed is not None
    assert renewed.current_period_end is not None
    assert original_end is not None
    assert as_utc(renewed.current_period_end) > as_utc(original_end)

    canceled = await subscriptions.cancel_for_telegram_user(telegram_user.id)
    assert canceled.cancel_at_period_end is True

    async with session_factory() as session, session.begin():
        subscription = await session.get(Subscription, claim.subscription.id, with_for_update=True)
        assert subscription is not None
        subscription.current_period_end = utc_now() - timedelta(minutes=1)

    assert await access.expire_due(grace_period_hours=0) == 1
    assert bot.banned_users == [telegram_user.id]
    async with session_factory() as session:
        stored_subscription = await session.get(Subscription, claim.subscription.id)
        membership = await session.scalar(select(ResourceMembership))
        assert stored_subscription is not None
        assert stored_subscription.status == SubscriptionStatus.EXPIRED
        assert membership is not None
        assert membership.status == MembershipStatus.REVOKED
