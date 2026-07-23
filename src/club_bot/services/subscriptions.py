from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from club_bot.domain.enums import (
    CheckoutStatus,
    PaymentStatus,
    ReferralStatus,
    SubscriptionStatus,
)
from club_bot.domain.rules import as_utc, generate_public_token, next_month, utc_now
from club_bot.integrations.wayforpay import WayForPayClient
from club_bot.models import CheckoutSession, Payment, Referral, Subscription, User
from club_bot.repositories import (
    CheckoutRepository,
    PlanRepository,
    SubscriptionRepository,
    UserRepository,
)
from club_bot.schemas import CheckoutResponse, SubscriptionView


class CheckoutNotFoundError(LookupError):
    pass


class PlanNotFoundError(LookupError):
    pass


class SubscriptionNotFoundError(LookupError):
    pass


class CheckoutExpiredError(RuntimeError):
    pass


class CheckoutOwnerNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class ClaimResult:
    paid: bool
    subscription: Subscription | None


class SubscriptionService:
    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        wayforpay: WayForPayClient,
        *,
        test_wayforpay: WayForPayClient | None = None,
        bot_username: str,
        service_url: str,
        default_return_url: str,
    ) -> None:
        self.session_factory = session_factory
        self.wayforpay = wayforpay
        self.test_wayforpay = test_wayforpay or wayforpay
        self.bot_username = bot_username
        self.service_url = service_url
        self.default_return_url = default_return_url

    async def create_checkout(
        self,
        *,
        plan_code: str,
        email: str | None,
        phone: str | None,
        referral_code: str | None,
        return_url: str | None,
        test_mode: bool = False,
        telegram_id: int | None = None,
    ) -> CheckoutResponse:
        now = utc_now()
        provider = self.test_wayforpay if test_mode else self.wayforpay
        async with self.session_factory() as session, session.begin():
            plan = await PlanRepository(session).by_code(plan_code)
            if plan is None:
                raise PlanNotFoundError(plan_code)
            user = None
            if telegram_id is not None:
                user = await UserRepository(session).by_telegram_id(telegram_id)
                if user is None:
                    raise CheckoutOwnerNotFoundError(telegram_id)
            token = generate_public_token()
            prefix = "TEST" if test_mode else "CLUB"
            order_reference = f"{prefix}-{now:%Y%m%d}-{token[:12]}"
            checkout = CheckoutSession(
                public_token=token,
                order_reference=order_reference,
                plan_id=plan.id,
                user_id=user.id if user is not None else None,
                referrer_code=referral_code,
                email=email,
                phone=phone,
                amount=plan.price,
                currency=plan.currency,
                expires_at=now + timedelta(hours=24),
            )
            session.add(checkout)
            if user is not None:
                await self._attach_referrer_from_checkout(session, user, referral_code)

            fields = provider.build_purchase_payload(
                order_reference=order_reference,
                order_date=int(now.timestamp()),
                amount=plan.price,
                currency=plan.currency,
                product_name=f"[TEST] {plan.name}" if test_mode else plan.name,
                service_url=self.service_url,
                return_url=return_url or self.default_return_url,
                date_next=next_month(now),
                email=email,
                phone=phone,
            )
            return CheckoutResponse(
                checkout_token=token,
                order_reference=order_reference,
                bot_claim_url=f"https://t.me/{self.bot_username}?start=claim_{token}",
                gateway_url=provider.checkout_url,
                gateway_fields=fields,
                expires_at=checkout.expires_at,
            )

    async def claim_checkout(self, token: str, telegram_id: int) -> ClaimResult:
        async with self.session_factory() as session, session.begin():
            checkout = await CheckoutRepository(session).by_token(token, for_update=True)
            if checkout is None:
                raise CheckoutNotFoundError(token)
            if (
                as_utc(checkout.expires_at) < utc_now()
                and checkout.status == CheckoutStatus.CREATED
            ):
                checkout.status = CheckoutStatus.EXPIRED
                raise CheckoutExpiredError(token)
            user = await UserRepository(session).by_telegram_id(telegram_id)
            if user is None:
                raise CheckoutNotFoundError("Telegram user must start the bot first")
            if checkout.user_id is not None and checkout.user_id != user.id:
                raise CheckoutNotFoundError("Checkout is already attached to another user")
            checkout.user_id = user.id
            await self._attach_referrer_from_checkout(session, user, checkout.referrer_code)
            if checkout.status not in (CheckoutStatus.PAID, CheckoutStatus.CLAIMED):
                return ClaimResult(paid=False, subscription=None)
            subscription = await self._activate_checkout(session, checkout)
            return ClaimResult(paid=True, subscription=subscription)

    async def process_callback(self, payload: dict[str, Any]) -> bool:
        """Persist a callback. Returns False when it is a duplicate delivery."""
        self.verify_callback(payload)
        event_id = self._callback_fingerprint(payload)
        async with self.session_factory() as session, session.begin():
            duplicate = await session.scalar(
                select(Payment.id).where(Payment.provider_event_id == event_id)
            )
            if duplicate is not None:
                return False

            order_reference = str(payload["orderReference"])
            rec_token = str(payload.get("recToken") or "") or None
            checkout = await CheckoutRepository(session).by_order_reference(
                order_reference, for_update=True
            )
            subscription = await SubscriptionRepository(session).by_provider_identifiers(
                order_reference, rec_token
            )
            provider_status = str(payload["transactionStatus"])
            approved = provider_status.casefold() == "approved"
            callback_amount = Decimal(str(payload["amount"])).quantize(Decimal("0.01"))
            callback_currency = str(payload["currency"]).upper()
            payment = Payment(
                subscription_id=subscription.id if subscription else None,
                checkout_session_id=checkout.id if checkout else None,
                provider_event_id=event_id,
                order_reference=order_reference,
                amount=callback_amount,
                currency=callback_currency,
                status=PaymentStatus.APPROVED if approved else PaymentStatus.DECLINED,
                paid_at=utc_now() if approved else None,
                failure_reason=None if approved else str(payload.get("reason", "Declined")),
                provider_payload=self._safe_provider_payload(payload),
            )
            session.add(payment)

            if not approved:
                if subscription and subscription.status == SubscriptionStatus.ACTIVE:
                    subscription.status = SubscriptionStatus.PAST_DUE
                return True

            expected_amount: Decimal | None = None
            expected_currency: str | None = None
            if checkout is not None:
                expected_amount = checkout.amount
                expected_currency = checkout.currency
            elif subscription is not None:
                expected_amount = subscription.billing_amount
                expected_currency = subscription.billing_currency
            if (
                expected_amount is not None
                and expected_currency is not None
                and (
                    callback_amount != expected_amount.quantize(Decimal("0.01"))
                    or callback_currency != expected_currency.upper()
                )
            ):
                payment.failure_reason = "Payment amount or currency mismatch"
                return True

            if checkout is not None and checkout.status != CheckoutStatus.CLAIMED:
                checkout.status = CheckoutStatus.PAID
                checkout.paid_at = utc_now()
                if checkout.user_id is not None:
                    subscription = await self._activate_checkout(
                        session, checkout, rec_token=rec_token
                    )
                    payment.subscription_id = subscription.id
            elif subscription is not None:
                self._extend_subscription(subscription, rec_token=rec_token)
                payment.subscription_id = subscription.id
            else:
                # The callback is genuine, but it cannot be matched. Keeping the payment
                # makes the issue visible to operators without granting access incorrectly.
                payment.failure_reason = "Unmatched approved payment"
            return True

    async def is_initial_checkout_callback(self, order_reference: str) -> bool:
        async with self.session_factory() as session:
            status = await session.scalar(
                select(CheckoutSession.status).where(
                    CheckoutSession.order_reference == order_reference
                )
            )
            return status is not None and status != CheckoutStatus.CLAIMED

    async def checkout_owner_telegram_id(self, order_reference: str) -> int | None:
        async with self.session_factory() as session:
            telegram_id = await session.scalar(
                select(User.telegram_id)
                .join(CheckoutSession, CheckoutSession.user_id == User.id)
                .where(
                    CheckoutSession.order_reference == order_reference,
                    CheckoutSession.status == CheckoutStatus.CLAIMED,
                )
            )
            return int(telegram_id) if telegram_id is not None else None

    async def checkout_owner_telegram_id_by_token(self, token: str) -> int | None:
        async with self.session_factory() as session:
            telegram_id = await session.scalar(
                select(User.telegram_id)
                .join(CheckoutSession, CheckoutSession.user_id == User.id)
                .where(CheckoutSession.public_token == token)
            )
            return int(telegram_id) if telegram_id is not None else None

    async def current_for_telegram_user(self, telegram_id: int) -> SubscriptionView | None:
        async with self.session_factory() as session:
            user = await UserRepository(session).by_telegram_id(telegram_id)
            if user is None:
                return None
            subscription = await SubscriptionRepository(session).current_for_user(user.id)
            if subscription is None:
                return None
            return SubscriptionView(
                plan_name=subscription.plan.name,
                status=subscription.status.value,
                current_period_end=subscription.current_period_end,
                cancel_at_period_end=subscription.cancel_at_period_end,
            )

    async def cancel_for_telegram_user(self, telegram_id: int) -> SubscriptionView:
        async with self.session_factory() as session:
            user = await UserRepository(session).by_telegram_id(telegram_id)
            if user is None:
                raise SubscriptionNotFoundError
            subscription = await SubscriptionRepository(session).current_for_user(user.id)
            if subscription is None or not subscription.provider_subscription_id:
                raise SubscriptionNotFoundError
            provider_id = subscription.provider_subscription_id

        # Do not hold a database transaction open during a network request.
        await self._provider_for_order(provider_id).suspend_recurring(provider_id)

        async with self.session_factory() as session, session.begin():
            user = await UserRepository(session).by_telegram_id(telegram_id)
            if user is None:
                raise SubscriptionNotFoundError
            subscription = await SubscriptionRepository(session).current_for_user(user.id)
            if subscription is None:
                raise SubscriptionNotFoundError
            subscription.cancel_at_period_end = True
            subscription.canceled_at = utc_now()
            return SubscriptionView(
                plan_name=subscription.plan.name,
                status=subscription.status.value,
                current_period_end=subscription.current_period_end,
                cancel_at_period_end=True,
            )

    def verify_callback(self, payload: dict[str, Any]) -> None:
        order_reference = str(payload.get("orderReference", ""))
        self._provider_for_order(order_reference).verify_callback(payload)

    def callback_response(self, order_reference: str) -> dict[str, str | int]:
        return self._provider_for_order(order_reference).callback_response(order_reference)

    def _provider_for_order(self, order_reference: str) -> WayForPayClient:
        return self.test_wayforpay if order_reference.startswith("TEST-") else self.wayforpay

    async def _activate_checkout(
        self,
        session: AsyncSession,
        checkout: CheckoutSession,
        *,
        rec_token: str | None = None,
    ) -> Subscription:
        if checkout.user_id is None:
            raise ValueError("Cannot activate an unclaimed checkout")
        existing = await session.scalar(
            select(Subscription)
            .options(selectinload(Subscription.plan))
            .where(Subscription.provider_subscription_id == checkout.order_reference)
        )
        if existing is not None:
            checkout.status = CheckoutStatus.CLAIMED
            return existing
        now = as_utc(checkout.paid_at) if checkout.paid_at else utc_now()
        subscription = Subscription(
            user_id=checkout.user_id,
            plan_id=checkout.plan_id,
            status=SubscriptionStatus.ACTIVE,
            current_period_start=now,
            current_period_end=next_month(now),
            billing_amount=checkout.amount,
            billing_currency=checkout.currency,
            provider=(
                "wayforpay_test"
                if checkout.order_reference.startswith("TEST-")
                else "wayforpay"
            ),
            provider_subscription_id=checkout.order_reference,
            provider_rec_token=rec_token,
        )
        session.add(subscription)
        checkout.status = CheckoutStatus.CLAIMED
        await session.flush()
        await self._qualify_referral(session, checkout.user_id)
        return subscription

    @staticmethod
    def _extend_subscription(subscription: Subscription, *, rec_token: str | None = None) -> None:
        now = utc_now()
        base = as_utc(subscription.current_period_end) if subscription.current_period_end else now
        if base < now:
            base = now
        subscription.current_period_start = base
        subscription.current_period_end = next_month(base)
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.cancel_at_period_end = False
        if rec_token:
            subscription.provider_rec_token = rec_token

    @staticmethod
    async def _attach_referrer_from_checkout(
        session: AsyncSession, user: User, referral_code: str | None
    ) -> None:
        if not referral_code or user.referred_by_user_id is not None:
            return
        referrer = await UserRepository(session).by_referral_code(referral_code)
        if referrer is None or referrer.id == user.id:
            return
        user.referred_by_user_id = referrer.id
        session.add(Referral(referrer_user_id=referrer.id, referred_user_id=user.id))

    @staticmethod
    async def _qualify_referral(session: AsyncSession, user_id: object) -> None:
        referral = await session.scalar(
            select(Referral).where(Referral.referred_user_id == user_id).with_for_update()
        )
        if referral and referral.status == ReferralStatus.REGISTERED:
            referral.status = ReferralStatus.QUALIFIED
            referral.qualified_at = utc_now()

    @staticmethod
    def _callback_fingerprint(payload: dict[str, Any]) -> str:
        values = (
            payload.get("orderReference"),
            payload.get("processingDate"),
            payload.get("authCode"),
            payload.get("transactionStatus"),
            payload.get("amount"),
            payload.get("reasonCode"),
        )
        return hashlib.sha256("|".join(map(str, values)).encode()).hexdigest()

    @staticmethod
    def _safe_provider_payload(payload: dict[str, Any]) -> dict[str, Any]:
        safe = dict(payload)
        safe.pop("merchantSignature", None)
        safe.pop("recToken", None)
        return safe
