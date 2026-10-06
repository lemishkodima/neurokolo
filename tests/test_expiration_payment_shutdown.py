from __future__ import annotations

import json
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from club_bot.db import create_engine, create_session_factory
from club_bot.domain.enums import (
    MembershipStatus,
    RecurringStatus,
    ResourceType,
    SubscriptionStatus,
)
from club_bot.domain.rules import utc_now
from club_bot.integrations.wayforpay import WayForPayClient
from club_bot.models import (
    Base,
    Plan,
    ResourceMembership,
    Subscription,
    TelegramResource,
    User,
)
from club_bot.services.access import AccessService
from club_bot.services.subscription_notifications import SubscriptionNotificationService
from club_bot.services.subscriptions import SubscriptionService


class FakeBot:
    def __init__(self) -> None:
        self.removed: list[int] = []
        self.messages: list[tuple[int, str]] = []

    async def revoke_chat_invite_link(self, *, chat_id: int, invite_link: str) -> None:
        return None

    async def ban_chat_member(self, *, chat_id: int, user_id: int) -> None:
        self.removed.append(user_id)

    async def unban_chat_member(
        self,
        *,
        chat_id: int,
        user_id: int,
        only_if_banned: bool,
    ) -> None:
        assert only_if_banned is True

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: object | None = None,
    ) -> None:
        self.messages.append((chat_id, text))


class FakeAdminService:
    async def list_admins(self) -> list[tuple[int, None]]:
        return [(999, None)]


def subscription_service(
    session_factory: async_sessionmaker[AsyncSession],
    client: WayForPayClient,
) -> SubscriptionService:
    return SubscriptionService(
        session_factory,
        client,
        bot_username="club_bot",
        service_url="https://api.example.com/webhooks/wayforpay",
        default_return_url="https://example.com/complete",
    )


async def seed_due_subscription(
    session_factory: async_sessionmaker[AsyncSession],
) -> tuple[Subscription, ResourceMembership]:
    async with session_factory() as session, session.begin():
        resource = TelegramResource(
            code="community",
            name="Community",
            chat_id=-100123,
            resource_type=ResourceType.SUPERGROUP,
        )
        plan = Plan(code="base", name="Base", price=610, resources=[resource])
        user = User(telegram_id=123, first_name="Member", referral_code="MEMBER123")
        session.add_all([resource, plan, user])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.PAST_DUE,
            current_period_start=utc_now() - timedelta(days=31),
            current_period_end=utc_now() - timedelta(minutes=1),
            billing_amount=610,
            billing_currency="UAH",
            provider="wayforpay",
            provider_subscription_id="CLUB-DUE",
            provider_recurring_status=RecurringStatus.ACTIVE.value,
        )
        membership = ResourceMembership(
            user_id=user.id,
            resource_id=resource.id,
            status=MembershipStatus.ACTIVE,
        )
        session.add_all([subscription, membership])
        await session.flush()
        return subscription, membership


async def test_active_rule_is_suspended_before_access_revocation(tmp_path: Path) -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_type = str(json.loads(request.content)["requestType"])
        requests.append(request_type)
        if request_type == "STATUS":
            return httpx.Response(
                200,
                json={"reasonCode": 4100, "reason": "Ok", "status": "Active"},
            )
        return httpx.Response(200, json={"reasonCode": 4100, "reason": "Ok"})

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'shutdown.db'}")
    session_factory = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WayForPayClient(
            merchant_account="merchant",
            merchant_domain="example.com",
            secret_key="secret",
            merchant_password="password",
            api_url="https://api.example.com/regularApi",
            checkout_url="https://secure.example.com/pay",
            http_client=http,
        )
        service = subscription_service(session_factory, client)
        subscription, membership = await seed_due_subscription(session_factory)
        bot: Any = FakeBot()
        access = AccessService(
            session_factory,
            bot,
            invite_ttl_seconds=3600,
            grace_period_hours=0,
            expiration_guard=service,
        )

        assert await access.expire_due(grace_period_hours=0) == 1
        assert requests == ["STATUS", "SUSPEND"]
        assert bot.removed == [123]
        async with session_factory() as session:
            stored = await session.get(Subscription, subscription.id)
            stored_membership = await session.get(ResourceMembership, membership.id)
            assert stored is not None
            assert stored.status == SubscriptionStatus.EXPIRED
            assert stored.provider_recurring_status == RecurringStatus.SUSPENDED.value
            assert stored.provider_expiration_shutdown_at is not None
            assert stored_membership is not None
            assert stored_membership.status == MembershipStatus.REVOKED
    await engine.dispose()


async def test_provider_failure_keeps_access_and_records_retryable_error(
    tmp_path: Path,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"reason": "Unavailable"})

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'failure.db'}")
    session_factory = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WayForPayClient(
            merchant_account="merchant",
            merchant_domain="example.com",
            secret_key="secret",
            merchant_password="password",
            api_url="https://api.example.com/regularApi",
            checkout_url="https://secure.example.com/pay",
            http_client=http,
        )
        service = subscription_service(session_factory, client)
        subscription, membership = await seed_due_subscription(session_factory)
        bot: Any = FakeBot()
        access = AccessService(
            session_factory,
            bot,
            invite_ttl_seconds=3600,
            grace_period_hours=0,
            expiration_guard=service,
        )

        assert await access.expire_due(grace_period_hours=0) == 0
        assert bot.removed == []
        notifier = SubscriptionNotificationService(
            bot,
            access,
            settings_service=Any,
            session_factory=session_factory,
            admin_service=FakeAdminService(),
        )
        assert await notifier.process_pending_expiration_shutdown_alerts() == 1
        assert bot.messages and bot.messages[0][0] == 999
        assert "Telegram-доступ поки не відкликано" in bot.messages[0][1]
        async with session_factory() as session:
            stored = await session.get(Subscription, subscription.id)
            stored_membership = await session.get(ResourceMembership, membership.id)
            assert stored is not None
            assert stored.status == SubscriptionStatus.PAST_DUE
            assert stored.provider_expiration_shutdown_at is None
            assert stored.provider_expiration_shutdown_error is not None
            assert stored_membership is not None
            assert stored_membership.status == MembershipStatus.ACTIVE
    await engine.dispose()


async def test_late_approved_reactivates_expired_subscription_and_resumes_rule(
    tmp_path: Path,
) -> None:
    requests: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        request_type = str(json.loads(request.content)["requestType"])
        requests.append(request_type)
        if request_type == "STATUS":
            return httpx.Response(
                200,
                json={"reasonCode": 4100, "reason": "Ok", "status": "Active"},
            )
        return httpx.Response(200, json={"reasonCode": 4100, "reason": "Ok"})

    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'late-approved.db'}")
    session_factory = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
        client = WayForPayClient(
            merchant_account="merchant",
            merchant_domain="example.com",
            secret_key="secret",
            merchant_password="password",
            api_url="https://api.example.com/regularApi",
            checkout_url="https://secure.example.com/pay",
            http_client=http,
        )
        service = subscription_service(session_factory, client)
        subscription, _ = await seed_due_subscription(session_factory)
        bot: Any = FakeBot()
        access = AccessService(
            session_factory,
            bot,
            invite_ttl_seconds=3600,
            grace_period_hours=0,
            expiration_guard=service,
        )
        assert await access.expire_due(grace_period_hours=0) == 1

        order_reference = "CLUB-DUE_WFPREG-123-1"
        context = await service.approved_callback_context(order_reference)
        assert context is not None
        assert context.restore_access is True
        assert context.resume_after_expiration is True
        callback: dict[str, Any] = {
            "merchantAccount": "merchant",
            "orderReference": order_reference,
            "amount": "610.00",
            "currency": "UAH",
            "authCode": "late-approved",
            "cardPan": "42****42",
            "transactionStatus": "Approved",
            "reasonCode": 1100,
            "processingDate": 1_799_999_999,
        }
        callback["merchantSignature"] = client._sign(
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
        assert await service.process_callback(callback) is True
        await service.recover_recurring_after_approved(order_reference, context)

        async with session_factory() as session:
            stored = await session.get(Subscription, subscription.id)
            assert stored is not None
            assert stored.status == SubscriptionStatus.ACTIVE
            assert stored.provider_recurring_status == RecurringStatus.ACTIVE.value
            assert stored.provider_expiration_shutdown_at is None
            assert stored.access_revoked_at is None
        assert requests == ["STATUS", "SUSPEND", "RESUME"]
    await engine.dispose()
