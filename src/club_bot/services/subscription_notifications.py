from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from html import escape
from zoneinfo import ZoneInfo

from aiogram import Bot
from aiogram.exceptions import TelegramAPIError
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from club_bot.domain.enums import PaymentStatus, SubscriptionStatus
from club_bot.domain.rules import as_utc, utc_now
from club_bot.models import CheckoutSession, Payment, Subscription
from club_bot.services.access import AccessService
from club_bot.services.admin import AdminService, SettingsService

logger = logging.getLogger(__name__)
KYIV_TIMEZONE = ZoneInfo("Europe/Kyiv")


@dataclass(frozen=True)
class DunningNotice:
    subscription_id: uuid.UUID
    telegram_id: int
    plan_name: str
    current_period_end: datetime
    repay_url: str | None
    user_notified_at: datetime | None


@dataclass(frozen=True)
class PaymentAdminAlert:
    payment_id: uuid.UUID
    telegram_id: int | None
    username: str | None
    plan_name: str | None
    amount: Decimal
    currency: str
    order_reference: str
    failure_reason: str | None


class SubscriptionNotificationService:
    def __init__(
        self,
        bot: Bot,
        access_service: AccessService,
        settings_service: SettingsService,
        session_factory: async_sessionmaker[AsyncSession] | None = None,
        admin_service: AdminService | None = None,
        *,
        grace_period_hours: int = 24,
        reminder_hours_before: int = 2,
    ) -> None:
        self.bot = bot
        self.access_service = access_service
        self.settings_service = settings_service
        self.session_factory = session_factory
        self.admin_service = admin_service
        self.grace_period_hours = grace_period_hours
        self.reminder_hours_before = reminder_hours_before

    async def send_activated(self, telegram_id: int) -> bool:
        try:
            invites = await self.access_service.create_invites(telegram_id)
        except Exception:
            logger.exception("Could not create subscription invites for %s", telegram_id)
            invites = []

        markup = None
        if invites:
            multiple = len(invites) > 1
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text=(f"{invite.name} 💎" if multiple else "Доєднатися 💎"),
                            url=invite.url,
                        )
                    ]
                    for invite in invites
                ]
            )
        text = await self.settings_service.get("payment_success_text")
        if invites:
            text += (
                "\n\nНатисніть кнопку нижче та подайте заявку. Бот автоматично "
                "підтвердить лише ваш Telegram-акаунт; посилання персональне."
            )
        else:
            text += "\n\nВідкрити доступ можна через кнопку «Матеріали»."
        try:
            await self.bot.send_message(telegram_id, text, reply_markup=markup)
        except TelegramAPIError:
            logger.exception("Could not notify Telegram user %s about activation", telegram_id)
            return False
        return True

    async def send_payment_failed(
        self,
        order_reference: str,
        rec_token: str | None = None,
    ) -> bool:
        if self.session_factory is None:
            return False
        notice = await self._notice_for_provider_identifiers(order_reference, rec_token)
        user_delivered = (
            await self._deliver_payment_failed(notice)
            if notice is not None
            else False
        )
        admin_delivered = await self.send_failed_payment_admin_alert(order_reference)
        return user_delivered or admin_delivered

    async def process_pending_payment_failures(self, *, limit: int = 100) -> int:
        if self.session_factory is None:
            return 0
        async with self.session_factory() as session:
            subscriptions = list(
                (
                    await session.scalars(
                        select(Subscription)
                        .options(selectinload(Subscription.user), selectinload(Subscription.plan))
                        .where(
                            Subscription.status == SubscriptionStatus.PAST_DUE,
                            Subscription.payment_failed_at.is_not(None),
                            Subscription.payment_failed_user_notified_at.is_(None),
                        )
                        .order_by(Subscription.payment_failed_at)
                        .limit(limit)
                    )
                ).all()
            )
            notices = [self._notice_from_subscription(item) for item in subscriptions]
        delivered = 0
        for notice in notices:
            if await self._deliver_payment_failed(notice):
                delivered += 1
        delivered += await self.process_pending_admin_alerts(limit=limit)
        return delivered

    async def send_failed_payment_admin_alert(self, order_reference: str) -> bool:
        if self.session_factory is None or self.admin_service is None:
            return False
        async with self.session_factory() as session:
            payment_id = await session.scalar(
                select(Payment.id)
                .where(
                    Payment.order_reference == order_reference,
                    Payment.status == PaymentStatus.DECLINED,
                    Payment.admin_notified_at.is_(None),
                    ~Payment.order_reference.startswith("TEST-"),
                )
                .order_by(Payment.created_at.desc())
                .limit(1)
            )
        return (
            await self._send_payment_admin_alert_by_id(payment_id)
            if payment_id is not None
            else False
        )

    async def process_pending_admin_alerts(self, *, limit: int = 100) -> int:
        if self.session_factory is None or self.admin_service is None:
            return 0
        async with self.session_factory() as session:
            payment_ids = list(
                (
                    await session.scalars(
                        select(Payment.id)
                        .where(
                            Payment.status == PaymentStatus.DECLINED,
                            Payment.admin_notified_at.is_(None),
                            ~Payment.order_reference.startswith("TEST-"),
                        )
                        .order_by(Payment.created_at)
                        .limit(limit)
                    )
                ).all()
            )
        delivered = 0
        for payment_id in payment_ids:
            if await self._send_payment_admin_alert_by_id(payment_id):
                delivered += 1
        return delivered

    async def send_recurring_rule_alert(
        self,
        order_reference: str,
        recurring_status: str,
    ) -> bool:
        if self.session_factory is None or self.admin_service is None:
            return False
        async with self.session_factory() as session, session.begin():
            subscription = await session.scalar(
                select(Subscription)
                .options(selectinload(Subscription.user), selectinload(Subscription.plan))
                .where(Subscription.provider_subscription_id == order_reference)
                .with_for_update()
            )
            if (
                subscription is None
                or subscription.provider != "wayforpay"
                or subscription.provider_recurring_status != recurring_status
                or subscription.provider_recurring_alerted_at is not None
                or recurring_status in {"active", "not_applicable"}
            ):
                return False
            subscription.provider_recurring_alerted_at = utc_now()
            telegram_id = int(subscription.user.telegram_id)
            username = subscription.user.username
            plan_name = subscription.plan.name
            amount = Decimal(subscription.billing_amount)
            currency = subscription.billing_currency
            reason = subscription.provider_recurring_reason

        username_text = f"@{escape(username)}" if username else "немає"
        text = (
            "🚨 <b>Approved-платіж без активної регулярки</b>\n\n"
            f"Користувач: <code>{telegram_id}</code> ({username_text})\n"
            f"Тариф: {escape(plan_name)}\n"
            f"Сума: {amount:.2f} {escape(currency)}\n"
            f"Order reference: <code>{escape(order_reference)}</code>\n"
            f"Recurring STATUS: <b>{escape(recurring_status)}</b>\n"
            f"Причина: {escape(reason or 'WayForPay не вказав причину')}\n\n"
            "Оплачений доступ активний, але наступне автоматичне списання "
            "не підтверджене WayForPay."
        )
        delivered = False
        for admin_id, _ in await self.admin_service.list_admins():
            try:
                await self.bot.send_message(admin_id, text, reply_markup=None)
                delivered = True
            except TelegramAPIError:
                logger.exception(
                    "Could not notify admin %s about recurring rule %s",
                    admin_id,
                    order_reference,
                )
        if not delivered:
            async with self.session_factory() as session, session.begin():
                subscription = await session.scalar(
                    select(Subscription)
                    .where(Subscription.provider_subscription_id == order_reference)
                    .with_for_update()
                )
                if subscription is not None:
                    subscription.provider_recurring_alerted_at = None
        return delivered

    async def send_due_grace_reminders(self, *, limit: int = 100) -> int:
        if self.session_factory is None or self.grace_period_hours <= 0:
            return 0
        now = utc_now()
        expiration_cutoff = now - timedelta(hours=self.grace_period_hours)
        reminder_lead = min(self.reminder_hours_before, self.grace_period_hours)
        reminder_cutoff = expiration_cutoff + timedelta(hours=reminder_lead)
        async with self.session_factory() as session:
            subscriptions = list(
                (
                    await session.scalars(
                        select(Subscription)
                        .options(selectinload(Subscription.user), selectinload(Subscription.plan))
                        .where(
                            Subscription.status == SubscriptionStatus.PAST_DUE,
                            Subscription.payment_failed_at.is_not(None),
                            Subscription.grace_reminder_notified_at.is_(None),
                            Subscription.current_period_end > expiration_cutoff,
                            Subscription.current_period_end <= reminder_cutoff,
                        )
                        .order_by(Subscription.current_period_end)
                        .limit(limit)
                    )
                ).all()
            )
            notices = [self._notice_from_subscription(item) for item in subscriptions]
        delivered = 0
        for notice in notices:
            grace_end = as_utc(notice.current_period_end) + timedelta(
                hours=self.grace_period_hours
            )
            text = (
                "⏳ <b>Доступ незабаром буде припинено</b>\n\n"
                "Оплату підписки не отримано. Пільговий період завершується "
                f"<b>{self._format_datetime(grace_end)}</b>.\n"
                "Повторіть оплату до цього часу, щоб зберегти доступ."
            )
            claimed = await self._claim_notification(
                notice.subscription_id,
                "grace_reminder_notified_at",
            )
            if claimed and await self._send_user(notice, text):
                delivered += 1
            elif claimed:
                await self._release_notification(
                    notice.subscription_id,
                    "grace_reminder_notified_at",
                )
        return delivered

    async def send_access_revoked_notifications(self, *, limit: int = 100) -> int:
        if self.session_factory is None:
            return 0
        async with self.session_factory() as session:
            subscriptions = list(
                (
                    await session.scalars(
                        select(Subscription)
                        .options(selectinload(Subscription.user), selectinload(Subscription.plan))
                        .where(
                            Subscription.status == SubscriptionStatus.EXPIRED,
                            Subscription.payment_failed_at.is_not(None),
                            Subscription.access_revoked_at.is_not(None),
                            Subscription.access_revoked_notified_at.is_(None),
                        )
                        .order_by(Subscription.access_revoked_at)
                        .limit(limit)
                    )
                ).all()
            )
            notices = [self._notice_from_subscription(item) for item in subscriptions]
        delivered = 0
        for notice in notices:
            text = (
                "🔒 <b>Підписку завершено</b>\n\n"
                f"Оплату тарифу «{escape(notice.plan_name)}» не отримано після "
                "пільгового періоду. Доступ до матеріалів відкликано."
            )
            claimed = await self._claim_notification(
                notice.subscription_id,
                "access_revoked_notified_at",
            )
            if claimed and await self._send_user(notice, text, include_repay=False):
                delivered += 1
            elif claimed:
                await self._release_notification(
                    notice.subscription_id,
                    "access_revoked_notified_at",
                )
        return delivered

    async def _deliver_payment_failed(self, notice: DunningNotice) -> bool:
        grace_end = as_utc(notice.current_period_end) + timedelta(
            hours=self.grace_period_hours
        )
        if notice.user_notified_at is None:
            text = (
                "⚠️ <b>Не вдалося продовжити підписку</b>\n\n"
                "WayForPay відхилив автоматичне списання. Доступ поки збережено до "
                f"<b>{self._format_datetime(grace_end)}</b>.\n"
            )
            if notice.repay_url:
                text += "Натисніть «Повторити оплату», щоб завершити платіж."
            else:
                text += (
                    "Поповніть картку: WayForPay автоматично повторить спробу "
                    "списання наступного дня."
                )
            claimed = await self._claim_notification(
                notice.subscription_id,
                "payment_failed_user_notified_at",
            )
            if claimed and await self._send_user(notice, text):
                return True
            if claimed:
                await self._release_notification(
                    notice.subscription_id,
                    "payment_failed_user_notified_at",
                )
        return False

    async def _send_payment_admin_alert_by_id(self, payment_id: uuid.UUID) -> bool:
        assert self.session_factory is not None
        assert self.admin_service is not None
        alert = await self._payment_admin_alert(payment_id)
        if alert is None or not await self._claim_payment_admin_alert(payment_id):
            return False
        username = f"@{escape(alert.username)}" if alert.username else "немає"
        telegram_id = str(alert.telegram_id) if alert.telegram_id is not None else "невідомий"
        plan_name = escape(alert.plan_name or "невідомий")
        reason = escape(alert.failure_reason or "WayForPay не вказав причину")
        text = (
            "🚨 <b>Невдалий production-платіж</b>\n\n"
            f"Користувач: <code>{telegram_id}</code> ({username})\n"
            f"Тариф: {plan_name}\n"
            f"Сума: {alert.amount:.2f} {escape(alert.currency)}\n"
            f"Order reference: <code>{escape(alert.order_reference)}</code>\n"
            f"Причина: {reason}"
        )
        delivered = False
        for admin_id, _ in await self.admin_service.list_admins():
            try:
                await self.bot.send_message(admin_id, text, reply_markup=None)
                delivered = True
            except TelegramAPIError:
                logger.exception(
                    "Could not notify admin %s about failed payment %s",
                    admin_id,
                    alert.order_reference,
                )
        if delivered:
            return True
        await self._release_payment_admin_alert(payment_id)
        return delivered

    async def _payment_admin_alert(
        self,
        payment_id: uuid.UUID,
    ) -> PaymentAdminAlert | None:
        assert self.session_factory is not None
        async with self.session_factory() as session:
            payment = await session.get(Payment, payment_id)
            if (
                payment is None
                or payment.admin_notified_at is not None
                or payment.status != PaymentStatus.DECLINED
                or payment.order_reference.startswith("TEST-")
            ):
                return None
            telegram_id: int | None = None
            username: str | None = None
            plan_name: str | None = None
            if payment.subscription_id is not None:
                subscription = await session.scalar(
                    select(Subscription)
                    .options(
                        selectinload(Subscription.user),
                        selectinload(Subscription.plan),
                    )
                    .where(Subscription.id == payment.subscription_id)
                )
                if subscription is not None:
                    telegram_id = int(subscription.user.telegram_id)
                    username = subscription.user.username
                    plan_name = subscription.plan.name
            elif payment.checkout_session_id is not None:
                checkout = await session.scalar(
                    select(CheckoutSession)
                    .options(
                        selectinload(CheckoutSession.user),
                        selectinload(CheckoutSession.plan),
                    )
                    .where(CheckoutSession.id == payment.checkout_session_id)
                )
                if checkout is not None:
                    telegram_id = (
                        int(checkout.user.telegram_id)
                        if checkout.user is not None
                        else None
                    )
                    username = checkout.user.username if checkout.user is not None else None
                    plan_name = checkout.plan.name
            return PaymentAdminAlert(
                payment_id=payment.id,
                telegram_id=telegram_id,
                username=username,
                plan_name=plan_name,
                amount=Decimal(payment.amount),
                currency=payment.currency,
                order_reference=payment.order_reference,
                failure_reason=payment.failure_reason,
            )

    async def _send_user(
        self,
        notice: DunningNotice,
        text: str,
        *,
        include_repay: bool = True,
    ) -> bool:
        markup = None
        if include_repay and notice.repay_url:
            markup = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Повторити оплату 💳",
                            url=notice.repay_url,
                        )
                    ]
                ]
            )
        try:
            await self.bot.send_message(notice.telegram_id, text, reply_markup=markup)
        except TelegramAPIError:
            logger.exception(
                "Could not send subscription notification to %s",
                notice.telegram_id,
            )
            return False
        return True

    async def _notice_for_provider_identifiers(
        self,
        order_reference: str,
        rec_token: str | None,
    ) -> DunningNotice | None:
        assert self.session_factory is not None
        conditions = [Subscription.provider_subscription_id == order_reference]
        if rec_token:
            conditions.append(Subscription.provider_rec_token == rec_token)
        async with self.session_factory() as session:
            subscription = await session.scalar(
                select(Subscription)
                .options(selectinload(Subscription.user), selectinload(Subscription.plan))
                .where(
                    or_(*conditions),
                    Subscription.status == SubscriptionStatus.PAST_DUE,
                    Subscription.payment_failed_at.is_not(None),
                )
            )
            return (
                self._notice_from_subscription(subscription)
                if subscription is not None
                else None
            )

    async def _claim_notification(
        self,
        subscription_id: uuid.UUID,
        field: str,
    ) -> bool:
        assert self.session_factory is not None
        async with self.session_factory() as session, session.begin():
            subscription = await session.get(Subscription, subscription_id, with_for_update=True)
            if subscription is None or getattr(subscription, field) is not None:
                return False
            if field in {
                "payment_failed_user_notified_at",
                "grace_reminder_notified_at",
            } and subscription.status != SubscriptionStatus.PAST_DUE:
                return False
            if (
                field == "access_revoked_notified_at"
                and subscription.status != SubscriptionStatus.EXPIRED
            ):
                return False
            setattr(subscription, field, utc_now())
            return True

    async def _release_notification(
        self,
        subscription_id: uuid.UUID,
        field: str,
    ) -> None:
        assert self.session_factory is not None
        async with self.session_factory() as session, session.begin():
            subscription = await session.get(Subscription, subscription_id, with_for_update=True)
            if subscription is not None:
                setattr(subscription, field, None)

    async def _claim_payment_admin_alert(self, payment_id: uuid.UUID) -> bool:
        assert self.session_factory is not None
        async with self.session_factory() as session, session.begin():
            payment = await session.get(Payment, payment_id, with_for_update=True)
            if (
                payment is None
                or payment.admin_notified_at is not None
                or payment.status != PaymentStatus.DECLINED
                or payment.order_reference.startswith("TEST-")
            ):
                return False
            payment.admin_notified_at = utc_now()
            return True

    async def _release_payment_admin_alert(self, payment_id: uuid.UUID) -> None:
        assert self.session_factory is not None
        async with self.session_factory() as session, session.begin():
            payment = await session.get(Payment, payment_id, with_for_update=True)
            if payment is not None:
                payment.admin_notified_at = None

    @staticmethod
    def _notice_from_subscription(subscription: Subscription) -> DunningNotice:
        if subscription.current_period_end is None:
            raise ValueError("A dunning notification requires current_period_end")
        return DunningNotice(
            subscription_id=subscription.id,
            telegram_id=int(subscription.user.telegram_id),
            plan_name=subscription.plan.name,
            current_period_end=as_utc(subscription.current_period_end),
            repay_url=subscription.provider_repay_url,
            user_notified_at=subscription.payment_failed_user_notified_at,
        )

    @staticmethod
    def _format_datetime(value: datetime) -> str:
        return as_utc(value).astimezone(KYIV_TIMEZONE).strftime("%d.%m.%Y о %H:%M")
