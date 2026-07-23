from __future__ import annotations

import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from club_bot.domain.enums import MembershipStatus, SubscriptionStatus
from club_bot.domain.rules import as_utc, utc_now
from club_bot.models import (
    Plan,
    ResourceMembership,
    Subscription,
    TelegramResource,
    User,
    plan_resources,
)
from club_bot.repositories import SubscriptionRepository, UserRepository


class AccessDeniedError(PermissionError):
    pass


@dataclass(frozen=True)
class ResourceInvite:
    name: str
    url: str


@dataclass(frozen=True)
class JoinRequestReview:
    membership_id: uuid.UUID
    allowed: bool


class AccessService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot,
        *,
        invite_ttl_seconds: int,
        grace_period_hours: int,
    ) -> None:
        self.session_factory = session_factory
        self.bot = bot
        self.invite_ttl_seconds = invite_ttl_seconds
        self.grace_period_hours = grace_period_hours

    async def create_invites(self, telegram_id: int) -> list[ResourceInvite]:
        async with self.session_factory() as session, session.begin():
            user = await UserRepository(session).by_telegram_id(telegram_id)
            if user is None:
                raise AccessDeniedError
            now = utc_now()
            entitlement_cutoff = now - timedelta(hours=self.grace_period_hours)
            subscriptions = list(
                (
                    await session.scalars(
                        select(Subscription)
                        .options(selectinload(Subscription.plan).selectinload(Plan.resources))
                        .where(
                            Subscription.user_id == user.id,
                            Subscription.status.in_(
                                [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]
                            ),
                            Subscription.current_period_end > entitlement_cutoff,
                        )
                    )
                ).all()
            )
            if not subscriptions:
                raise AccessDeniedError

            resources_by_id = {
                resource.id: resource
                for subscription in subscriptions
                for resource in subscription.plan.resources
                if resource.is_active
            }
            resources = sorted(
                resources_by_id.values(),
                key=lambda item: item.sort_order,
            )
            invites: list[ResourceInvite] = []
            expires_at = now + timedelta(seconds=self.invite_ttl_seconds)
            for resource in resources:
                membership = await session.scalar(
                    select(ResourceMembership).where(
                        ResourceMembership.user_id == user.id,
                        ResourceMembership.resource_id == resource.id,
                    )
                )
                if membership is not None and membership.invite_link:
                    with suppress(TelegramBadRequest):
                        await self.bot.revoke_chat_invite_link(
                            chat_id=resource.chat_id,
                            invite_link=membership.invite_link,
                        )
                link = await self.bot.create_chat_invite_link(
                    chat_id=resource.chat_id,
                    name=f"user:{telegram_id}",
                    expire_date=expires_at,
                    creates_join_request=True,
                )
                if membership is None:
                    membership = ResourceMembership(
                        user_id=user.id,
                        resource_id=resource.id,
                    )
                    session.add(membership)
                membership.status = MembershipStatus.INVITED
                membership.invite_link = link.invite_link
                membership.invite_expires_at = expires_at
                membership.joined_at = None
                membership.revoked_at = None
                invites.append(ResourceInvite(name=resource.name, url=link.invite_link))
            return invites

    async def handle_join_request(
        self,
        *,
        chat_id: int,
        telegram_id: int,
        invite_link: str | None,
    ) -> bool:
        review = (
            await self._review_join_request(
                chat_id=chat_id,
                telegram_id=telegram_id,
                invite_link=invite_link,
            )
            if invite_link is not None
            else None
        )
        if review is not None and review.allowed and invite_link is not None:
            await self.bot.approve_chat_join_request(
                chat_id=chat_id,
                user_id=telegram_id,
            )
            with suppress(TelegramBadRequest):
                await self.bot.revoke_chat_invite_link(
                    chat_id=chat_id,
                    invite_link=invite_link,
                )
            await self._mark_joined(review.membership_id, invite_link)
            return True

        with suppress(TelegramBadRequest):
            await self.bot.decline_chat_join_request(
                chat_id=chat_id,
                user_id=telegram_id,
            )
        if review is not None and invite_link is not None:
            with suppress(TelegramBadRequest):
                await self.bot.revoke_chat_invite_link(
                    chat_id=chat_id,
                    invite_link=invite_link,
                )
            await self._clear_pending_invite(review.membership_id, invite_link)
        return False

    async def _review_join_request(
        self,
        *,
        chat_id: int,
        telegram_id: int,
        invite_link: str,
    ) -> JoinRequestReview | None:
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(
                        ResourceMembership.id,
                        ResourceMembership.user_id,
                        ResourceMembership.resource_id,
                        ResourceMembership.status,
                        ResourceMembership.invite_expires_at,
                        User.telegram_id,
                    )
                    .join(User, User.id == ResourceMembership.user_id)
                    .join(
                        TelegramResource,
                        TelegramResource.id == ResourceMembership.resource_id,
                    )
                    .where(
                        TelegramResource.chat_id == chat_id,
                        TelegramResource.is_active.is_(True),
                        ResourceMembership.invite_link == invite_link,
                    )
                )
            ).one_or_none()
            if row is None:
                return None

            now = utc_now()
            identity_matches = int(row.telegram_id) == telegram_id
            invite_is_current = (
                row.status == MembershipStatus.INVITED
                and row.invite_expires_at is not None
                and as_utc(row.invite_expires_at) > now
            )
            entitlement = None
            if identity_matches and invite_is_current:
                entitlement_cutoff = now - timedelta(hours=self.grace_period_hours)
                entitlement = await session.scalar(
                    select(Subscription.id)
                    .join(
                        plan_resources,
                        Subscription.plan_id == plan_resources.c.plan_id,
                    )
                    .where(
                        Subscription.user_id == row.user_id,
                        Subscription.status.in_(
                            [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]
                        ),
                        Subscription.current_period_end > entitlement_cutoff,
                        plan_resources.c.resource_id == row.resource_id,
                    )
                    .limit(1)
                )
            return JoinRequestReview(
                membership_id=row.id,
                allowed=identity_matches and invite_is_current and entitlement is not None,
            )

    async def _mark_joined(self, membership_id: uuid.UUID, invite_link: str) -> None:
        async with self.session_factory() as session, session.begin():
            membership = await session.get(
                ResourceMembership,
                membership_id,
                with_for_update=True,
            )
            if membership is None or membership.invite_link != invite_link:
                return
            membership.status = MembershipStatus.ACTIVE
            membership.joined_at = utc_now()
            membership.invite_link = None
            membership.invite_expires_at = None
            membership.revoked_at = None

    async def _clear_pending_invite(
        self,
        membership_id: uuid.UUID,
        invite_link: str,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            membership = await session.get(
                ResourceMembership,
                membership_id,
                with_for_update=True,
            )
            if membership is None or membership.invite_link != invite_link:
                return
            membership.invite_link = None
            membership.invite_expires_at = None

    async def revoke_subscription_access(
        self,
        subscription_id: object,
        *,
        entitlement_cutoff: datetime | None = None,
    ) -> None:
        cutoff = entitlement_cutoff if entitlement_cutoff is not None else utc_now()
        async with self.session_factory() as session, session.begin():
            subscription = await session.scalar(
                select(Subscription)
                .options(
                    selectinload(Subscription.user),
                    selectinload(Subscription.plan).selectinload(Plan.resources),
                )
                .where(Subscription.id == subscription_id)
                .with_for_update()
            )
            if subscription is None:
                return
            if (
                subscription.status
                not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE)
                or subscription.current_period_end is None
                or as_utc(subscription.current_period_end) > cutoff
            ):
                return
            for resource in subscription.plan.resources:
                other_entitlement = await session.scalar(
                    select(Subscription.id)
                    .join(
                        plan_resources,
                        Subscription.plan_id == plan_resources.c.plan_id,
                    )
                    .where(
                        Subscription.user_id == subscription.user_id,
                        Subscription.id != subscription.id,
                        Subscription.status.in_(
                            [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]
                        ),
                        Subscription.current_period_end > cutoff,
                        plan_resources.c.resource_id == resource.id,
                    )
                    .limit(1)
                )
                if other_entitlement is not None:
                    continue
                await self._remove_member(resource, subscription.user.telegram_id)
                membership = await session.scalar(
                    select(ResourceMembership).where(
                        ResourceMembership.user_id == subscription.user_id,
                        ResourceMembership.resource_id == resource.id,
                    )
                )
                if membership:
                    if membership.invite_link:
                        # The link can already be expired/revoked or owned by another admin.
                        with suppress(TelegramBadRequest):
                            await self.bot.revoke_chat_invite_link(
                                chat_id=resource.chat_id,
                                invite_link=membership.invite_link,
                            )
                    membership.status = MembershipStatus.REVOKED
                    membership.revoked_at = utc_now()
                    membership.invite_link = None
            subscription.status = SubscriptionStatus.EXPIRED

    async def expire_due(self, *, grace_period_hours: int, limit: int = 100) -> int:
        cutoff = utc_now() - timedelta(hours=grace_period_hours)
        async with self.session_factory() as session, session.begin():
            due = await SubscriptionRepository(session).expired(cutoff, limit=limit)
            ids = [item.id for item in due]
        for subscription_id in ids:
            await self.revoke_subscription_access(
                subscription_id,
                entitlement_cutoff=cutoff,
            )
        return len(ids)

    async def _remove_member(self, resource: TelegramResource, telegram_id: int) -> None:
        try:
            await self.bot.ban_chat_member(chat_id=resource.chat_id, user_id=telegram_id)
            await self.bot.unban_chat_member(
                chat_id=resource.chat_id,
                user_id=telegram_id,
                only_if_banned=True,
            )
        except TelegramBadRequest as error:
            # "user not found" means there is no access left to revoke. Permission
            # errors are re-raised because they require operator intervention.
            if "user not found" not in str(error).casefold():
                raise
