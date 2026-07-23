from __future__ import annotations

from datetime import datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from club_bot.domain.enums import SubscriptionStatus
from club_bot.models import CheckoutSession, Plan, Subscription, User


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def by_telegram_id(self, telegram_id: int) -> User | None:
        result: User | None = await self.session.scalar(
            select(User).where(User.telegram_id == telegram_id)
        )
        return result

    async def by_referral_code(self, code: str) -> User | None:
        result: User | None = await self.session.scalar(
            select(User).where(User.referral_code == code)
        )
        return result


class PlanRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def by_code(self, code: str, *, active_only: bool = True) -> Plan | None:
        statement = select(Plan).where(Plan.code == code)
        if active_only:
            statement = statement.where(Plan.is_active.is_(True))
        result: Plan | None = await self.session.scalar(statement)
        return result


class CheckoutRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def by_token(self, token: str, *, for_update: bool = False) -> CheckoutSession | None:
        statement = (
            select(CheckoutSession)
            .options(selectinload(CheckoutSession.plan))
            .where(CheckoutSession.public_token == token)
        )
        if for_update:
            statement = statement.with_for_update()
        result: CheckoutSession | None = await self.session.scalar(statement)
        return result

    async def by_order_reference(
        self, order_reference: str, *, for_update: bool = False
    ) -> CheckoutSession | None:
        statement = (
            select(CheckoutSession)
            .options(selectinload(CheckoutSession.plan))
            .where(CheckoutSession.order_reference == order_reference)
        )
        if for_update:
            statement = statement.with_for_update()
        result: CheckoutSession | None = await self.session.scalar(statement)
        return result


class SubscriptionRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def current_for_user(self, user_id: object) -> Subscription | None:
        result: Subscription | None = await self.session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.plan).selectinload(Plan.resources))
            .where(
                Subscription.user_id == user_id,
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]),
            )
            .order_by(Subscription.current_period_end.desc())
            .limit(1)
        )
        return result

    async def by_provider_identifiers(
        self, order_reference: str, rec_token: str | None
    ) -> Subscription | None:
        conditions = [Subscription.provider_subscription_id == order_reference]
        if rec_token:
            conditions.append(Subscription.provider_rec_token == rec_token)
        from sqlalchemy import or_

        result: Subscription | None = await self.session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(or_(*conditions))
            .with_for_update()
        )
        return result

    async def expired(self, cutoff: datetime, *, limit: int = 100) -> list[Subscription]:
        result = await self.session.scalars(
            select(Subscription)
            .options(selectinload(Subscription.user), selectinload(Subscription.plan))
            .where(
                Subscription.status.in_([SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]),
                Subscription.current_period_end <= cutoff,
            )
            .order_by(Subscription.current_period_end)
            .limit(limit)
            .with_for_update(skip_locked=True)
        )
        return list(result.all())
