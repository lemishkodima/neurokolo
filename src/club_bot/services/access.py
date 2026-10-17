from __future__ import annotations

import json
import logging
import uuid
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta
from html import escape
from typing import Any, Protocol

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from club_bot.domain.enums import MembershipStatus, SubscriptionStatus
from club_bot.domain.rules import as_utc, utc_now
from club_bot.models import (
    AccessEvent,
    AppSetting,
    Plan,
    ResourceMembership,
    Subscription,
    TelegramResource,
    User,
    plan_resources,
)
from club_bot.repositories import SubscriptionRepository
from club_bot.services.admin import AdminService

logger = logging.getLogger(__name__)


class AccessDeniedError(PermissionError):
    pass


class AccessRecoveryError(RuntimeError):
    pass


@dataclass(frozen=True)
class ResourceInvite:
    name: str
    url: str


@dataclass(frozen=True)
class JoinRequestReview:
    membership_id: uuid.UUID
    allowed: bool


class ExpirationPreparation(Protocol):
    @property
    def ready(self) -> bool: ...


class ExpirationGuard(Protocol):
    async def prepare_subscription_for_expiration(
        self,
        subscription_id: object,
        cutoff: datetime,
    ) -> ExpirationPreparation: ...


class AccessService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        bot: Bot,
        *,
        invite_ttl_seconds: int,
        grace_period_hours: int,
        expiration_guard: ExpirationGuard | None = None,
        admin_service: AdminService | None = None,
        admin_alerts_enabled: bool = True,
    ) -> None:
        self.session_factory = session_factory
        self.bot = bot
        self.invite_ttl_seconds = invite_ttl_seconds
        self.grace_period_hours = grace_period_hours
        self.expiration_guard = expiration_guard
        self.admin_service = admin_service
        self.admin_alerts_enabled = admin_alerts_enabled

    async def create_invites(self, telegram_id: int) -> list[ResourceInvite]:
        try:
            invites = await self._create_invites(telegram_id)
        except AccessDeniedError:
            raise
        except Exception as error:
            reason = str(error) if isinstance(error, AccessRecoveryError) else type(error).__name__
            await self._alert_access_error(telegram_id, reason)
            raise AccessRecoveryError("Не вдалося підготувати доступ") from error
        if invites:
            await self._resolve_access_alert(f"grant:{telegram_id}")
        return invites

    async def _create_invites(self, telegram_id: int) -> list[ResourceInvite]:
        async with self.session_factory() as session, session.begin():
            # Serialize concurrent payment/menu requests, including the first invite.
            user = await session.scalar(
                select(User).where(User.telegram_id == telegram_id).with_for_update()
            )
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
                await self._ensure_not_banned(resource.chat_id, telegram_id)
                membership = await session.scalar(
                    select(ResourceMembership).where(
                        ResourceMembership.user_id == user.id,
                        ResourceMembership.resource_id == resource.id,
                    )
                )
                if (
                    membership is not None
                    and membership.status == MembershipStatus.INVITED
                    and membership.invite_link
                    and membership.invite_expires_at is not None
                    and as_utc(membership.invite_expires_at) > now + timedelta(seconds=30)
                ):
                    invites.append(ResourceInvite(name=resource.name, url=membership.invite_link))
                    continue
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

    async def record_access_event(
        self, chat_id: int, telegram_id: int, action: str,
        *, actor_id: int | None = None, occurred_at: datetime | None = None,
    ) -> None:
        # No FK to the outer transaction: a Telegram side effect survives DB rollback.
        async with self.session_factory() as session, session.begin():
            session.add(AccessEvent(
                chat_id=chat_id, telegram_id=telegram_id, action=action,
                actor_id=actor_id, occurred_at=occurred_at or utc_now(),
            ))

    async def _owns_ban(self, chat_id: int, telegram_id: int) -> bool:
        async with self.session_factory() as session:
            event = await session.scalar(
                select(AccessEvent).where(
                    AccessEvent.chat_id == chat_id, AccessEvent.telegram_id == telegram_id,
                    AccessEvent.action.in_(["bot_ban", "bot_unban", "manual_ban", "manual_unban"]),
                ).order_by(AccessEvent.occurred_at.desc()).limit(1)
            )
            return event is not None and event.action == "bot_ban"

    async def _ensure_not_banned(self, chat_id: int, telegram_id: int) -> None:
        member = await self.bot.get_chat_member(chat_id=chat_id, user_id=telegram_id)
        if member.status != "kicked":
            return
        if not await self._owns_ban(chat_id, telegram_id):
            await self.record_access_event(chat_id, telegram_id, "unknown_or_manual_ban")
            raise AccessRecoveryError("Блокування потребує перевірки адміністратора")
        await self._verified_unban(chat_id, telegram_id)

    async def _verified_unban(self, chat_id: int, telegram_id: int) -> None:
        await self.record_access_event(chat_id, telegram_id, "unban_requested")
        await self.bot.unban_chat_member(
            chat_id=chat_id, user_id=telegram_id, only_if_banned=True,
        )
        member = await self.bot.get_chat_member(chat_id=chat_id, user_id=telegram_id)
        if member.status == "kicked":
            await self.record_access_event(chat_id, telegram_id, "unban_failed")
            raise AccessRecoveryError("Telegram не підтвердив розблокування")
        await self.record_access_event(chat_id, telegram_id, "bot_unban")

    async def _alert_access_error(
        self, telegram_id: int, reason: str, *, subscription_id: object | None = None,
    ) -> None:
        if not self.admin_alerts_enabled:
            logger.warning("Access alert muted for Telegram ID %s: %s", telegram_id, reason)
            return
        if self.admin_service is None:
            return
        operation = "revoke" if subscription_id is not None else "grant"
        scope = (f"revoke:{subscription_id}" if subscription_id is not None
                 else f"grant:{telegram_id}")
        try:
            admins = await self.admin_service.list_admins()
            async with self.session_factory() as session, session.begin():
                await session.scalar(select(User).where(
                    User.telegram_id == telegram_id,
                ).with_for_update())
                key = f"access_notice:{scope}"
                setting = await session.get(AppSetting, key, with_for_update=True)
                if setting is None:
                    setting = AppSetting(key=key, value="{}")
                    session.add(setting)
                    state: dict[str, Any] = {"telegram_id": telegram_id, "operation": operation,
                             "notified": [], "resolved": False}
                else:
                    state = json.loads(setting.value)
                title = ("⚠️ Помилка відкликання доступу до клубу." if operation == "revoke"
                         else "⚠️ Помилка видачі доступу до клубу.")
                detail = (
                    "Не вдалося завершити видалення з каналу або перевірку розблокування. "
                    "Це не видача доступу після оплати. Бот повторить спробу автоматично."
                    if operation == "revoke" else
                    "Не вдалося підготувати персональне посилання входу. "
                    "Повторна оплата не потрібна. Після усунення причини користувач "
                    "може повторити «Доєднатися»."
                )
                for admin_id, _ in admins:
                    if admin_id in state["notified"]:
                        continue
                    try:
                        await self.bot.send_message(
                            admin_id, f"{title}\nTelegram ID: <code>{telegram_id}</code>\n"
                            f"Причина: {escape(reason)}.\n{detail}",
                        )
                        state["notified"].append(admin_id)
                    except TelegramAPIError:
                        logger.exception("Could not deliver access alert to admin %s", admin_id)
                setting.value = json.dumps(state)
        except Exception:
            logger.exception("Could not persist access alert %s", scope)

    async def _resolve_access_alert(self, scope: str) -> None:
        try:
            async with self.session_factory() as session, session.begin():
                await self._mark_access_alert_resolved(session, scope)
            await self.deliver_access_resolutions()
        except Exception:
            logger.exception("Could not resolve access alert %s", scope)

    async def _mark_access_alert_resolved(self, session: AsyncSession, scope: str) -> None:
        setting = await session.get(AppSetting, f"access_notice:{scope}", with_for_update=True)
        if setting is not None:
            state = json.loads(setting.value)
            state["resolved"] = True
            setting.value = json.dumps(state)

    async def deliver_access_resolutions(self) -> None:
        if not self.admin_alerts_enabled or self.admin_service is None:
            return
        admins = {item[0] for item in await self.admin_service.list_admins()}
        async with self.session_factory() as session:
            keys = list((await session.scalars(select(AppSetting.key).where(
                AppSetting.key.startswith("access_notice:"),
            ))).all())
        for key in keys:
            async with self.session_factory() as session, session.begin():
                setting = await session.get(AppSetting, key, with_for_update=True)
                if setting is None:
                    continue
                state = json.loads(setting.value)
                if not state["resolved"]:
                    continue
                detail = (
                    "Операцію відкликання доступу завершено. Розблокування після "
                    "видалення не повертає користувача в канал."
                    if state["operation"] == "revoke" else
                    "Персональне посилання входу успішно підготовлено. "
                    "Це ще не підтвердження фактичного вступу користувача в канал."
                )
                pending = []
                for admin_id in state["notified"]:
                    if admin_id not in admins:
                        continue
                    try:
                        await self.bot.send_message(
                            admin_id, "✅ Проблему вирішено: "
                            + ("відкликання доступу" if state["operation"] == "revoke"
                               else "видача доступу")
                            + f".\nTelegram ID: <code>{state['telegram_id']}</code>\n{detail}",
                        )
                    except TelegramAPIError:
                        pending.append(admin_id)
                        logger.exception("Could not deliver access recovery to admin %s", admin_id)
                if pending:
                    state["notified"] = pending
                    setting.value = json.dumps(state)
                else:
                    await session.delete(setting)

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
    ) -> bool:
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
                return False
            if (
                subscription.status
                not in (SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE)
                or subscription.current_period_end is None
                or as_utc(subscription.current_period_end) > cutoff
            ):
                return False
            fully_revoked = True
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
                removed = await self._remove_member(
                    resource,
                    subscription.user.telegram_id,
                )
                if not removed:
                    fully_revoked = False
                    continue
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
            subscription.access_revoked_at = utc_now() if fully_revoked else None
            if fully_revoked:
                await self._mark_access_alert_resolved(session, f"revoke:{subscription_id}")
            return fully_revoked

    async def expire_due(self, *, grace_period_hours: int, limit: int = 100) -> int:
        cutoff = utc_now() - timedelta(hours=grace_period_hours)
        async with self.session_factory() as session, session.begin():
            due = await SubscriptionRepository(session).expired(cutoff, limit=limit)
            ids = [item.id for item in due]
        revoked = 0
        for subscription_id in ids:
            try:
                if self.expiration_guard is not None:
                    preparation = (
                        await self.expiration_guard.prepare_subscription_for_expiration(
                            subscription_id,
                            cutoff,
                        )
                    )
                    if not preparation.ready:
                        continue
                fully_revoked = await self.revoke_subscription_access(
                    subscription_id,
                    entitlement_cutoff=cutoff,
                )
            except Exception as error:
                logger.exception(
                    "Could not revoke expired subscription %s; continuing with the batch",
                    subscription_id,
                )
                async with self.session_factory() as session:
                    telegram_id = await session.scalar(
                        select(User.telegram_id).join(Subscription, Subscription.user_id == User.id)
                        .where(Subscription.id == subscription_id)
                    )
                if telegram_id is not None:
                    reason = (str(error) if isinstance(error, AccessRecoveryError)
                              else type(error).__name__)
                    await self._alert_access_error(
                        telegram_id, reason, subscription_id=subscription_id,
                    )
                continue
            if fully_revoked:
                revoked += 1
        return revoked

    async def _remove_member(self, resource: TelegramResource, telegram_id: int) -> bool:
        try:
            member = await self.bot.get_chat_member(chat_id=resource.chat_id, user_id=telegram_id)
            if member.status == "kicked":
                if await self._owns_ban(resource.chat_id, telegram_id):
                    await self._verified_unban(resource.chat_id, telegram_id)
                return True
            await self.record_access_event(resource.chat_id, telegram_id, "ban_requested")
            await self.bot.ban_chat_member(chat_id=resource.chat_id, user_id=telegram_id)
            await self.record_access_event(resource.chat_id, telegram_id, "bot_ban")
            await self._verified_unban(resource.chat_id, telegram_id)
            return True
        except TelegramBadRequest as error:
            message = str(error).casefold()
            if "can't remove chat owner" in message:
                logger.error(
                    "Cannot revoke Telegram owner %s from resource %s (%s)",
                    telegram_id,
                    resource.name,
                    resource.chat_id,
                )
                return False
            # Other permission errors require operator intervention and remain retryable.
            raise
