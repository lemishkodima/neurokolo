from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from club_bot.bot.system_router import review_join_request, system_router
from club_bot.db import create_engine, create_session_factory
from club_bot.domain.enums import MembershipStatus, ResourceType, SubscriptionStatus
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
        self.created: list[dict[str, Any]] = []
        self.revoked: list[tuple[int, str]] = []
        self.approved: list[tuple[int, int]] = []
        self.declined: list[tuple[int, int]] = []

    async def create_chat_invite_link(self, **kwargs: Any) -> SimpleNamespace:
        self.created.append(kwargs)
        return SimpleNamespace(invite_link=f"https://t.me/+personal-{len(self.created)}")

    async def revoke_chat_invite_link(self, *, chat_id: int, invite_link: str) -> None:
        self.revoked.append((chat_id, invite_link))

    async def approve_chat_join_request(self, *, chat_id: int, user_id: int) -> None:
        self.approved.append((chat_id, user_id))

    async def decline_chat_join_request(self, *, chat_id: int, user_id: int) -> None:
        self.declined.append((chat_id, user_id))


async def _access_service(
    tmp_path: Path,
) -> tuple[
    AsyncEngine,
    async_sessionmaker[AsyncSession],
    AccessService,
    FakeBot,
    object,
]:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'join-requests.db'}")
    session_factory = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    async with session_factory() as session, session.begin():
        resource = TelegramResource(
            code="community",
            name="Community",
            chat_id=-100123,
            resource_type=ResourceType.SUPERGROUP,
        )
        plan = Plan(
            code="base",
            name="Base",
            price=990,
            currency="UAH",
            resources=[resource],
        )
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
            current_period_start=utc_now(),
            current_period_end=utc_now() + timedelta(days=30),
            billing_amount=990,
            billing_currency="UAH",
            provider_subscription_id="CLUB-JOIN",
        )
        session.add(subscription)
        await session.flush()
        subscription_id = subscription.id
    bot = FakeBot()
    access = AccessService(
        session_factory,
        bot,  # type: ignore[arg-type]
        invite_ttl_seconds=3600,
        grace_period_hours=0,
    )
    return engine, session_factory, access, bot, subscription_id


async def test_join_request_invite_replaces_old_link_and_approves_only_owner(
    tmp_path: Path,
) -> None:
    engine, session_factory, access, bot, _subscription_id = await _access_service(tmp_path)

    first = await access.create_invites(123)
    second = await access.create_invites(123)

    assert first[0].url == "https://t.me/+personal-1"
    assert second[0].url == "https://t.me/+personal-2"
    assert bot.created[0]["creates_join_request"] is True
    assert "member_limit" not in bot.created[0]
    assert bot.revoked == [(-100123, first[0].url)]

    assert await access.handle_join_request(
        chat_id=-100123,
        telegram_id=123,
        invite_link=second[0].url,
    )
    assert bot.approved == [(-100123, 123)]
    assert bot.declined == []
    assert bot.revoked[-1] == (-100123, second[0].url)

    async with session_factory() as session:
        membership = await session.scalar(select(ResourceMembership))
        assert membership is not None
        assert membership.status == MembershipStatus.ACTIVE
        assert membership.joined_at is not None
        assert membership.invite_link is None
        assert membership.invite_expires_at is None
    await engine.dispose()


async def test_foreign_join_request_is_declined_and_compromised_link_revoked(
    tmp_path: Path,
) -> None:
    engine, session_factory, access, bot, _subscription_id = await _access_service(tmp_path)
    invite = (await access.create_invites(123))[0]

    assert (
        await access.handle_join_request(
            chat_id=-100123,
            telegram_id=999,
            invite_link=invite.url,
        )
        is False
    )
    assert bot.approved == []
    assert bot.declined == [(-100123, 999)]
    assert bot.revoked == [(-100123, invite.url)]

    async with session_factory() as session:
        membership = await session.scalar(select(ResourceMembership))
        assert membership is not None
        assert membership.status == MembershipStatus.INVITED
        assert membership.invite_link is None
        assert membership.invite_expires_at is None
    await engine.dispose()


async def test_join_request_rechecks_entitlement_before_approval(tmp_path: Path) -> None:
    engine, session_factory, access, bot, subscription_id = await _access_service(tmp_path)
    invite = (await access.create_invites(123))[0]
    async with session_factory() as session, session.begin():
        subscription = await session.get(Subscription, subscription_id)
        assert subscription is not None
        subscription.current_period_end = utc_now() - timedelta(seconds=1)

    assert (
        await access.handle_join_request(
            chat_id=-100123,
            telegram_id=123,
            invite_link=invite.url,
        )
        is False
    )
    assert bot.approved == []
    assert bot.declined == [(-100123, 123)]
    assert bot.revoked == [(-100123, invite.url)]
    await engine.dispose()


async def test_system_router_forwards_join_request_identity_and_link() -> None:
    class FakeAccessService:
        def __init__(self) -> None:
            self.request: dict[str, object] | None = None

        async def handle_join_request(self, **kwargs: object) -> bool:
            self.request = kwargs
            return True

    access = FakeAccessService()
    event = SimpleNamespace(
        chat=SimpleNamespace(id=-100123),
        from_user=SimpleNamespace(id=123),
        invite_link=SimpleNamespace(invite_link="https://t.me/+personal"),
    )

    await review_join_request(
        event,  # type: ignore[arg-type]
        access,  # type: ignore[arg-type]
    )

    assert access.request == {
        "chat_id": -100123,
        "telegram_id": 123,
        "invite_link": "https://t.me/+personal",
    }
    assert "chat_join_request" in system_router.resolve_used_update_types()
