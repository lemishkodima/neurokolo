from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from club_bot.db import create_engine, create_session_factory
from club_bot.domain.enums import (
    MembershipStatus,
    ResourceType,
    SubscriptionStatus,
)
from club_bot.domain.rules import utc_now
from club_bot.models import (
    Base,
    Plan,
    ResourceMembership,
    Subscription,
    TelegramResource,
    User,
)
from club_bot.services.access import AccessService


class FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[str, int, int]] = []

    async def revoke_chat_invite_link(self, *, chat_id: int, invite_link: str) -> None:
        self.calls.append(("revoke", chat_id, 0))

    async def ban_chat_member(self, *, chat_id: int, user_id: int) -> None:
        self.calls.append(("ban", chat_id, user_id))

    async def unban_chat_member(self, *, chat_id: int, user_id: int, only_if_banned: bool) -> None:
        assert only_if_banned is True
        self.calls.append(("unban", chat_id, user_id))


async def test_expired_subscription_is_removed_from_all_resources(tmp_path: Path) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'access.db'}")
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session, session.begin():
        resource = TelegramResource(
            code="community",
            name="Community",
            chat_id=-100123,
            resource_type=ResourceType.SUPERGROUP,
        )
        plan = Plan(code="base", name="Base", price=990, resources=[resource])
        user = User(
            telegram_id=123,
            first_name="Member",
            referral_code="MEMBER123",
        )
        session.add_all([resource, plan, user])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=utc_now() - timedelta(days=31),
            current_period_end=utc_now() - timedelta(minutes=1),
            billing_amount=990,
            billing_currency="UAH",
            provider_subscription_id="CLUB-1",
        )
        membership = ResourceMembership(
            user_id=user.id,
            resource_id=resource.id,
            status=MembershipStatus.ACTIVE,
            invite_link="https://t.me/+private",
        )
        session.add_all([subscription, membership])

    bot: Any = FakeBot()
    access = AccessService(
        session_factory,
        bot,
        invite_ttl_seconds=3600,
        grace_period_hours=0,
    )
    assert await access.expire_due(grace_period_hours=0) == 1
    assert ("ban", -100123, 123) in bot.calls
    assert ("unban", -100123, 123) in bot.calls

    async with session_factory() as session:
        stored = await session.get(Subscription, subscription.id)
        stored_membership = await session.get(ResourceMembership, membership.id)
        assert stored is not None and stored.status == SubscriptionStatus.EXPIRED
        assert stored.access_revoked_at is not None
        assert stored_membership is not None
        assert stored_membership.status == MembershipStatus.REVOKED

    await engine.dispose()


async def test_expiration_preserves_access_covered_by_another_subscription(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'overlap.db'}")
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    async with session_factory() as session, session.begin():
        resource = TelegramResource(
            code="community",
            name="Community",
            chat_id=-100123,
            resource_type=ResourceType.SUPERGROUP,
        )
        plan = Plan(code="base", name="Base", price=990, currency="UAH", resources=[resource])
        user = User(telegram_id=123, first_name="Member", referral_code="MEMBER123")
        session.add_all([resource, plan, user])
        await session.flush()
        expired = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=utc_now() - timedelta(days=31),
            current_period_end=utc_now() - timedelta(minutes=1),
            billing_amount=990,
            billing_currency="UAH",
            provider_subscription_id="CLUB-OLD",
        )
        current = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=utc_now(),
            current_period_end=utc_now() + timedelta(days=30),
            billing_amount=990,
            billing_currency="UAH",
            provider_subscription_id="CLUB-NEW",
        )
        membership = ResourceMembership(
            user_id=user.id,
            resource_id=resource.id,
            status=MembershipStatus.ACTIVE,
        )
        session.add_all([expired, current, membership])

    bot: Any = FakeBot()
    access = AccessService(
        session_factory,
        bot,
        invite_ttl_seconds=3600,
        grace_period_hours=0,
    )
    assert await access.expire_due(grace_period_hours=0) == 1
    assert not bot.calls

    async with session_factory() as session:
        stored_expired = await session.get(Subscription, expired.id)
        stored_current = await session.get(Subscription, current.id)
        stored_membership = await session.get(ResourceMembership, membership.id)
        assert stored_expired is not None
        assert stored_expired.status == SubscriptionStatus.EXPIRED
        assert stored_current is not None
        assert stored_current.status == SubscriptionStatus.ACTIVE
        assert stored_membership is not None
        assert stored_membership.status == MembershipStatus.ACTIVE

    await engine.dispose()
