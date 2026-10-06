from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any
from urllib.parse import urlsplit

from sqlalchemy import or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.orm import selectinload

from club_bot.domain.billing import add_billing_months, wayforpay_regular_mode
from club_bot.domain.enums import (
    CheckoutStatus,
    PaymentStatus,
    RecurringStatus,
    ReferralStatus,
    SubscriptionStatus,
)
from club_bot.domain.payments import (
    WAYFORPAY_RECURRING_REFERENCE_MARKER,
    canonical_wayforpay_order_reference,
)
from club_bot.domain.rules import as_utc, generate_public_token, utc_now
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


@dataclass(frozen=True)
class RecurringAuditResult:
    order_reference: str
    status: RecurringStatus

    @property
    def requires_alert(self) -> bool:
        return self.status not in {
            RecurringStatus.ACTIVE,
            RecurringStatus.NOT_APPLICABLE,
        }


@dataclass(frozen=True)
class RecurringReconciliationResult:
    scanned: int
    matched: int
    approved: int
    declined: int
    amount_mismatches: int
    missing_subscriptions: int
    applied: bool


@dataclass(frozen=True)
class ExpirationPreparationResult:
    subscription_id: uuid.UUID
    ready: bool
    failure_reason: str | None = None


@dataclass(frozen=True)
class ApprovedCallbackContext:
    telegram_id: int
    restore_access: bool
    resume_after_expiration: bool
    preserve_cancellation: bool


@dataclass(frozen=True)
class ExpiredRecurringReconciliationResult:
    scanned: int
    active: int
    suspended: int
    terminal: int
    failed: int
    applied: bool


UNMATCHED_APPROVED_PAYMENT_REASON = "Unmatched approved payment"
EXPIRATION_SUSPEND_REASON = "Auto-suspended before Telegram access revocation"


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
        regular_count: int = 24,
    ) -> None:
        self.session_factory = session_factory
        self.wayforpay = wayforpay
        self.test_wayforpay = test_wayforpay or wayforpay
        self.bot_username = bot_username
        self.service_url = service_url
        self.default_return_url = default_return_url
        self.regular_count = regular_count

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
                billing_months=plan.billing_months,
                expires_at=now + timedelta(hours=24),
            )
            session.add(checkout)
            if user is not None:
                await self._attach_referrer_from_checkout(session, user, referral_code)

            next_payment_at = add_billing_months(now, plan.billing_months)
            period_label = (
                f"{plan.billing_months} місяць"
                if plan.billing_months == 1
                else f"{plan.billing_months} місяців"
            )
            fields = provider.build_purchase_payload(
                order_reference=order_reference,
                order_date=int(now.timestamp()),
                amount=plan.price,
                currency=plan.currency,
                product_name=(
                    f"[TEST] {plan.name} — підписка на {period_label}, автопродовження"
                    if test_mode
                    else f"{plan.name} — підписка на {period_label}, автопродовження"
                ),
                service_url=self.service_url,
                return_url=return_url or self.default_return_url,
                date_next=next_payment_at,
                regular_count=self.regular_count,
                regular_mode=wayforpay_regular_mode(plan.billing_months),
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
            approved_payload = await session.scalar(
                select(Payment.provider_payload)
                .where(
                    Payment.checkout_session_id == checkout.id,
                    Payment.status == PaymentStatus.APPROVED,
                    Payment.failure_reason.is_(None),
                )
                .order_by(Payment.created_at.desc())
                .limit(1)
            )
            subscription = await self._activate_checkout(
                session,
                checkout,
                repay_url=self._repay_url_from_payload(approved_payload or {}),
            )
            await session.execute(
                update(Payment)
                .where(
                    Payment.checkout_session_id == checkout.id,
                    Payment.status == PaymentStatus.APPROVED,
                    Payment.subscription_id.is_(None),
                    Payment.failure_reason.is_(None),
                )
                .values(subscription_id=subscription.id)
            )
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
                if subscription and subscription.status in (
                    SubscriptionStatus.ACTIVE,
                    SubscriptionStatus.PAST_DUE,
                ):
                    self._apply_failed_payment(
                        subscription,
                        failure_reason=payment.failure_reason,
                        failed_at=utc_now(),
                        payload=payload,
                    )
                    await self._restore_repay_url(session, subscription)
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
                        session,
                        checkout,
                        rec_token=rec_token,
                        repay_url=self._repay_url_from_payload(payload),
                    )
                    payment.subscription_id = subscription.id
            elif subscription is not None:
                self._extend_subscription(
                    subscription,
                    rec_token=rec_token,
                    repay_url=self._repay_url_from_payload(payload),
                )
                payment.subscription_id = subscription.id
            else:
                # The callback is genuine, but it cannot be matched. Keeping the payment
                # makes the issue visible to operators without granting access incorrectly.
                payment.failure_reason = UNMATCHED_APPROVED_PAYMENT_REASON
            return True

    async def reconcile_unmatched_recurring_callbacks(
        self,
        *,
        apply: bool = False,
    ) -> RecurringReconciliationResult:
        """Link and apply previously persisted WayForPay recurring callbacks.

        Only unlinked callbacks containing WayForPay's recurring reference marker are
        considered. Linking the payment makes the operation idempotent on repeated runs.
        """
        scanned = 0
        matched = 0
        approved = 0
        declined = 0
        amount_mismatches = 0
        missing_subscriptions = 0
        async with self.session_factory() as session, session.begin():
            payments = list(
                (
                    await session.scalars(
                        select(Payment)
                        .where(
                            Payment.subscription_id.is_(None),
                            Payment.order_reference.contains(
                                WAYFORPAY_RECURRING_REFERENCE_MARKER
                            ),
                        )
                        .order_by(Payment.created_at, Payment.id)
                        .with_for_update(skip_locked=True)
                    )
                ).all()
            )
            scanned = len(payments)
            for payment in payments:
                canonical_reference = canonical_wayforpay_order_reference(
                    payment.order_reference
                )
                subscription = await session.scalar(
                    select(Subscription)
                    .where(
                        Subscription.provider_subscription_id == canonical_reference
                    )
                    .with_for_update()
                )
                if subscription is None:
                    missing_subscriptions += 1
                    continue
                if (
                    Decimal(payment.amount).quantize(Decimal("0.01"))
                    != Decimal(subscription.billing_amount).quantize(Decimal("0.01"))
                    or payment.currency.upper() != subscription.billing_currency.upper()
                ):
                    amount_mismatches += 1
                    continue

                matched += 1
                if payment.status == PaymentStatus.APPROVED:
                    approved += 1
                    if apply:
                        payment.subscription_id = subscription.id
                        payment.failure_reason = None
                        self._extend_subscription(
                            subscription,
                            effective_at=as_utc(payment.created_at),
                        )
                elif payment.status == PaymentStatus.DECLINED:
                    declined += 1
                    if apply:
                        payment.subscription_id = subscription.id
                        if subscription.status in {
                            SubscriptionStatus.ACTIVE,
                            SubscriptionStatus.PAST_DUE,
                        }:
                            self._apply_failed_payment(
                                subscription,
                                failure_reason=payment.failure_reason,
                                failed_at=as_utc(payment.created_at),
                                payload=payment.provider_payload,
                            )

        return RecurringReconciliationResult(
            scanned=scanned,
            matched=matched,
            approved=approved,
            declined=declined,
            amount_mismatches=amount_mismatches,
            missing_subscriptions=missing_subscriptions,
            applied=apply,
        )

    async def prepare_subscription_for_expiration(
        self,
        subscription_id: object,
        cutoff: datetime,
    ) -> ExpirationPreparationResult:
        """Stop a production recurring rule before Telegram access is revoked."""
        cutoff = as_utc(cutoff)
        async with self.session_factory() as session:
            subscription = await session.get(Subscription, subscription_id)
            if subscription is None:
                raise SubscriptionNotFoundError
            if not self._is_due_for_expiration(subscription, cutoff):
                return ExpirationPreparationResult(subscription.id, ready=False)
            if subscription.provider != "wayforpay":
                return ExpirationPreparationResult(subscription.id, ready=True)
            order_reference = subscription.provider_subscription_id
            if not order_reference:
                reason = "Production subscription has no provider subscription reference"
                await self._record_expiration_shutdown_failure(subscription.id, reason)
                return ExpirationPreparationResult(
                    subscription.id,
                    ready=False,
                    failure_reason=reason,
                )

        try:
            data = await self.wayforpay.recurring_status(order_reference)
        except Exception as error:
            reason = f"{type(error).__name__}: recurring STATUS failed before expiration"
            await self._record_expiration_shutdown_failure(subscription_id, reason)
            return ExpirationPreparationResult(
                subscription.id,
                ready=False,
                failure_reason=reason,
            )

        result, provider_reason = self._recurring_result(order_reference, data)
        terminal_statuses = {
            RecurringStatus.SUSPENDED,
            RecurringStatus.REMOVED,
            RecurringStatus.COMPLETED,
            RecurringStatus.MISSING,
        }
        issued_suspend = False
        final_status = result.status
        final_reason = provider_reason
        if result.status in {
            RecurringStatus.ACTIVE,
            RecurringStatus.CREATED,
            RecurringStatus.CONFIRMED,
        }:
            try:
                await self.wayforpay.suspend_recurring(order_reference)
            except Exception as error:
                reason = f"{type(error).__name__}: recurring SUSPEND failed before expiration"
                await self._record_expiration_shutdown_failure(
                    subscription_id,
                    reason,
                    recurring_status=result.status,
                )
                return ExpirationPreparationResult(
                    subscription.id,
                    ready=False,
                    failure_reason=reason,
                )
            issued_suspend = True
            final_status = RecurringStatus.SUSPENDED
            final_reason = EXPIRATION_SUSPEND_REASON
        elif result.status not in terminal_statuses:
            reason = f"WayForPay STATUS could not prove shutdown: {provider_reason}"
            await self._record_expiration_shutdown_failure(
                subscription_id,
                reason,
                recurring_status=result.status,
            )
            return ExpirationPreparationResult(
                subscription.id,
                ready=False,
                failure_reason=reason,
            )

        stale_after_provider_call = False
        preserve_cancellation = False
        async with self.session_factory() as session, session.begin():
            stored = await session.get(Subscription, subscription_id, with_for_update=True)
            if stored is None:
                raise SubscriptionNotFoundError
            if not self._is_due_for_expiration(stored, cutoff):
                stale_after_provider_call = True
                preserve_cancellation = stored.cancel_at_period_end
            else:
                now = utc_now()
                stored.provider_recurring_status = final_status.value
                stored.provider_recurring_checked_at = now
                stored.provider_recurring_reason = final_reason[:1000]
                stored.provider_expiration_shutdown_at = now
                stored.provider_expiration_shutdown_error = None
                stored.provider_expiration_shutdown_alerted_at = None

        if stale_after_provider_call:
            if issued_suspend and not preserve_cancellation:
                await self._resume_after_expiration_race(subscription_id, order_reference)
            return ExpirationPreparationResult(subscription.id, ready=False)
        return ExpirationPreparationResult(subscription.id, ready=True)

    async def approved_callback_context(
        self,
        order_reference: str,
    ) -> ApprovedCallbackContext | None:
        canonical_reference = canonical_wayforpay_order_reference(order_reference)
        async with self.session_factory() as session:
            row = (
                await session.execute(
                    select(Subscription, User.telegram_id)
                    .join(User, User.id == Subscription.user_id)
                    .where(Subscription.provider_subscription_id == canonical_reference)
                )
            ).one_or_none()
            if row is None:
                return None
            subscription, telegram_id = row
            return ApprovedCallbackContext(
                telegram_id=int(telegram_id),
                restore_access=(
                    subscription.status == SubscriptionStatus.EXPIRED
                    or subscription.access_revoked_at is not None
                ),
                resume_after_expiration=(
                    subscription.provider_expiration_shutdown_at is not None
                    and not subscription.cancel_at_period_end
                ),
                preserve_cancellation=subscription.cancel_at_period_end,
            )

    async def recover_recurring_after_approved(
        self,
        order_reference: str,
        context: ApprovedCallbackContext,
    ) -> None:
        canonical_reference = canonical_wayforpay_order_reference(order_reference)
        if not context.resume_after_expiration and not context.preserve_cancellation:
            return
        try:
            if context.preserve_cancellation:
                await self.wayforpay.suspend_recurring(canonical_reference)
                status = RecurringStatus.SUSPENDED
                reason = "Renewal raced with user cancellation; rule suspended again"
            else:
                await self.wayforpay.resume_recurring(canonical_reference)
                status = RecurringStatus.ACTIVE
                reason = "Recurring rule resumed after late approved renewal"
        except Exception as error:
            async with self.session_factory() as session, session.begin():
                subscription = await session.scalar(
                    select(Subscription)
                    .where(Subscription.provider_subscription_id == canonical_reference)
                    .with_for_update()
                )
                if subscription is not None:
                    subscription.provider_recurring_status = RecurringStatus.CHECK_FAILED.value
                    subscription.provider_recurring_checked_at = utc_now()
                    subscription.provider_recurring_reason = (
                        f"{type(error).__name__}: recurring recovery after Approved failed"
                    )
            return

        async with self.session_factory() as session, session.begin():
            subscription = await session.scalar(
                select(Subscription)
                .where(Subscription.provider_subscription_id == canonical_reference)
                .with_for_update()
            )
            if subscription is None:
                return
            now = utc_now()
            subscription.provider_recurring_status = status.value
            subscription.provider_recurring_checked_at = now
            subscription.provider_recurring_reason = reason
            subscription.provider_expiration_shutdown_at = (
                now if context.preserve_cancellation else None
            )
            subscription.provider_expiration_shutdown_error = None
            subscription.provider_expiration_shutdown_alerted_at = None
            if context.preserve_cancellation:
                subscription.cancel_at_period_end = True
                subscription.canceled_at = subscription.canceled_at or now

    async def reconcile_expired_recurring_rules(
        self,
        *,
        apply: bool = False,
    ) -> ExpiredRecurringReconciliationResult:
        """Audit legacy expired subscriptions and optionally stop chargeable rules."""
        async with self.session_factory() as session:
            subscription_ids = list(
                (
                    await session.scalars(
                        select(Subscription.id)
                        .where(
                            Subscription.provider == "wayforpay",
                            Subscription.status == SubscriptionStatus.EXPIRED,
                            Subscription.provider_subscription_id.is_not(None),
                        )
                        .order_by(Subscription.current_period_end, Subscription.id)
                    )
                ).all()
            )

        active = 0
        suspended = 0
        terminal = 0
        failed = 0
        terminal_statuses = {
            RecurringStatus.REMOVED,
            RecurringStatus.COMPLETED,
            RecurringStatus.MISSING,
        }
        for subscription_id in subscription_ids:
            issued_suspend = False
            async with self.session_factory() as session:
                subscription = await session.get(Subscription, subscription_id)
                if (
                    subscription is None
                    or subscription.status != SubscriptionStatus.EXPIRED
                    or not subscription.provider_subscription_id
                ):
                    continue
                order_reference = subscription.provider_subscription_id
            try:
                data = await self.wayforpay.recurring_status(order_reference)
                result, provider_reason = self._recurring_result(order_reference, data)
            except Exception:
                failed += 1
                continue
            if result.status == RecurringStatus.ACTIVE:
                active += 1
                if not apply:
                    continue
                async with self.session_factory() as session:
                    still_expired = await session.scalar(
                        select(Subscription.id).where(
                            Subscription.id == subscription_id,
                            Subscription.status == SubscriptionStatus.EXPIRED,
                        )
                    )
                if still_expired is None:
                    continue
                try:
                    await self.wayforpay.suspend_recurring(order_reference)
                except Exception:
                    failed += 1
                    continue
                result = RecurringAuditResult(
                    order_reference=order_reference,
                    status=RecurringStatus.SUSPENDED,
                )
                issued_suspend = True
                provider_reason = EXPIRATION_SUSPEND_REASON
                suspended += 1
            elif result.status == RecurringStatus.SUSPENDED:
                suspended += 1
            elif result.status in terminal_statuses:
                terminal += 1
            else:
                failed += 1
                continue

            if apply:
                resume_after_race = False
                async with self.session_factory() as session, session.begin():
                    stored = await session.get(
                        Subscription,
                        subscription_id,
                        with_for_update=True,
                    )
                    if stored is None or stored.status != SubscriptionStatus.EXPIRED:
                        resume_after_race = issued_suspend
                    else:
                        now = utc_now()
                        stored.provider_recurring_status = result.status.value
                        stored.provider_recurring_checked_at = now
                        stored.provider_recurring_reason = provider_reason[:1000]
                        stored.provider_expiration_shutdown_at = now
                        stored.provider_expiration_shutdown_error = None
                        stored.provider_expiration_shutdown_alerted_at = None
                if resume_after_race:
                    await self.wayforpay.resume_recurring(order_reference)

        return ExpiredRecurringReconciliationResult(
            scanned=len(subscription_ids),
            active=active,
            suspended=suspended,
            terminal=terminal,
            failed=failed,
            applied=apply,
        )

    async def is_initial_checkout_callback(self, order_reference: str) -> bool:
        canonical_reference = canonical_wayforpay_order_reference(order_reference)
        async with self.session_factory() as session:
            status = await session.scalar(
                select(CheckoutSession.status).where(
                    CheckoutSession.order_reference == canonical_reference
                )
            )
            return status is not None and status != CheckoutStatus.CLAIMED

    async def checkout_owner_telegram_id(self, order_reference: str) -> int | None:
        canonical_reference = canonical_wayforpay_order_reference(order_reference)
        async with self.session_factory() as session:
            telegram_id = await session.scalar(
                select(User.telegram_id)
                .join(CheckoutSession, CheckoutSession.user_id == User.id)
                .where(
                    CheckoutSession.order_reference == canonical_reference,
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
        subscriptions = await self.current_subscriptions_for_telegram_user(telegram_id)
        return subscriptions[0] if subscriptions else None

    async def current_subscriptions_for_telegram_user(
        self,
        telegram_id: int,
    ) -> list[SubscriptionView]:
        async with self.session_factory() as session:
            user = await UserRepository(session).by_telegram_id(telegram_id)
            if user is None:
                return []
            subscriptions = await SubscriptionRepository(session).current_all_for_user(user.id)
            return [self._subscription_view(subscription) for subscription in subscriptions]

    async def cancel_for_telegram_user(self, telegram_id: int) -> SubscriptionView:
        async with self.session_factory() as session:
            user = await UserRepository(session).by_telegram_id(telegram_id)
            if user is None:
                raise SubscriptionNotFoundError
            subscription = await SubscriptionRepository(session).current_for_user(user.id)
            if subscription is None or not subscription.provider_subscription_id:
                raise SubscriptionNotFoundError
            provider_id = subscription.provider_subscription_id
            test_subscription = (
                subscription.provider == "wayforpay_test" or provider_id.startswith("TEST-")
            )

        # Do not hold a database transaction open during a network request.
        # The public test merchant approves checkout callbacks but doesn't create
        # a regular-payment rule, so SUSPEND would return 4102 ("Rule is not found").
        if not test_subscription:
            await self.wayforpay.suspend_recurring(provider_id)

        async with self.session_factory() as session, session.begin():
            user = await UserRepository(session).by_telegram_id(telegram_id)
            if user is None:
                raise SubscriptionNotFoundError
            subscription = await SubscriptionRepository(session).current_for_user(user.id)
            if subscription is None:
                raise SubscriptionNotFoundError
            subscription.cancel_at_period_end = True
            subscription.canceled_at = utc_now()
            subscription.provider_recurring_status = (
                RecurringStatus.NOT_APPLICABLE.value
                if test_subscription
                else RecurringStatus.SUSPENDED.value
            )
            subscription.provider_recurring_checked_at = utc_now()
            subscription.provider_expiration_shutdown_at = utc_now()
            subscription.provider_expiration_shutdown_error = None
            subscription.provider_expiration_shutdown_alerted_at = None
            return SubscriptionView(
                plan_name=subscription.plan.name,
                billing_amount=subscription.billing_amount,
                billing_currency=subscription.billing_currency,
                billing_months=subscription.billing_months,
                status=subscription.status.value,
                current_period_end=subscription.current_period_end,
                cancel_at_period_end=True,
                auto_renew_enabled=False,
                provider_recurring_status=subscription.provider_recurring_status,
            )

    @staticmethod
    def _subscription_view(subscription: Subscription) -> SubscriptionView:
        return SubscriptionView(
            plan_name=subscription.plan.name,
            billing_amount=subscription.billing_amount,
            billing_currency=subscription.billing_currency,
            billing_months=subscription.billing_months,
            status=subscription.status.value,
            current_period_end=subscription.current_period_end,
            cancel_at_period_end=subscription.cancel_at_period_end,
            auto_renew_enabled=(
                not subscription.cancel_at_period_end
                and subscription.provider_recurring_status
                == RecurringStatus.ACTIVE.value
            ),
            provider_recurring_status=subscription.provider_recurring_status,
        )

    async def verify_recurring_for_order(
        self,
        order_reference: str,
    ) -> RecurringAuditResult | None:
        order_reference = canonical_wayforpay_order_reference(order_reference)
        async with self.session_factory() as session:
            subscription = await session.scalar(
                select(Subscription).where(
                    Subscription.provider_subscription_id == order_reference
                )
            )
            if subscription is None:
                return None
            subscription_id = subscription.id
            provider = subscription.provider

        if provider != "wayforpay":
            result = RecurringAuditResult(
                order_reference=order_reference,
                status=RecurringStatus.NOT_APPLICABLE,
            )
            reason = "Test subscriptions do not create a production recurring rule"
        else:
            try:
                data = await self.wayforpay.recurring_status(order_reference)
            except Exception as error:
                result = RecurringAuditResult(
                    order_reference=order_reference,
                    status=RecurringStatus.CHECK_FAILED,
                )
                reason = f"{type(error).__name__}: recurring STATUS request failed"
            else:
                result, reason = self._recurring_result(order_reference, data)

        async with self.session_factory() as session, session.begin():
            subscription = await session.get(Subscription, subscription_id, with_for_update=True)
            if subscription is None:
                return None
            subscription.provider_recurring_status = result.status.value
            subscription.provider_recurring_checked_at = utc_now()
            subscription.provider_recurring_reason = reason[:1000]
            if result.status == RecurringStatus.ACTIVE:
                subscription.provider_recurring_alerted_at = None
        return result

    async def audit_unverified_recurring_rules(
        self,
        *,
        recheck_minutes: int,
        limit: int = 5,
    ) -> list[RecurringAuditResult]:
        cutoff = utc_now() - timedelta(minutes=recheck_minutes)
        async with self.session_factory() as session:
            order_references = list(
                (
                    await session.scalars(
                        select(Subscription.provider_subscription_id)
                        .where(
                            Subscription.provider == "wayforpay",
                            Subscription.provider_subscription_id.is_not(None),
                            Subscription.status.in_(
                                [SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE]
                            ),
                            Subscription.cancel_at_period_end.is_(False),
                            or_(
                                Subscription.provider_recurring_status.is_(None),
                                Subscription.provider_recurring_status
                                != RecurringStatus.ACTIVE.value,
                            ),
                            or_(
                                Subscription.provider_recurring_checked_at.is_(None),
                                Subscription.provider_recurring_checked_at <= cutoff,
                            ),
                        )
                        .order_by(Subscription.provider_recurring_checked_at.asc().nullsfirst())
                        .limit(limit)
                    )
                ).all()
            )
        results: list[RecurringAuditResult] = []
        for order_reference in order_references:
            if order_reference is None:
                continue
            result = await self.verify_recurring_for_order(order_reference)
            if result is not None:
                results.append(result)
        return results

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
        repay_url: str | None = None,
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
            current_period_end=add_billing_months(now, checkout.billing_months),
            billing_amount=checkout.amount,
            billing_currency=checkout.currency,
            billing_months=checkout.billing_months,
            provider=(
                "wayforpay_test"
                if checkout.order_reference.startswith("TEST-")
                else "wayforpay"
            ),
            provider_subscription_id=checkout.order_reference,
            provider_rec_token=rec_token,
            provider_repay_url=repay_url,
            provider_recurring_status=(
                RecurringStatus.NOT_APPLICABLE.value
                if checkout.order_reference.startswith("TEST-")
                else RecurringStatus.PENDING.value
            ),
        )
        session.add(subscription)
        checkout.status = CheckoutStatus.CLAIMED
        await session.flush()
        await self._qualify_referral(session, checkout.user_id)
        return subscription

    @staticmethod
    def _extend_subscription(
        subscription: Subscription,
        *,
        rec_token: str | None = None,
        repay_url: str | None = None,
        effective_at: datetime | None = None,
    ) -> None:
        effective_time = as_utc(effective_at) if effective_at is not None else utc_now()
        base = (
            as_utc(subscription.current_period_end)
            if subscription.current_period_end
            else effective_time
        )
        if base < effective_time:
            base = effective_time
        subscription.current_period_start = base
        subscription.current_period_end = add_billing_months(
            base,
            subscription.billing_months,
        )
        subscription.status = SubscriptionStatus.ACTIVE
        subscription.cancel_at_period_end = False
        subscription.payment_failed_at = None
        subscription.payment_failure_reason = None
        subscription.payment_failed_user_notified_at = None
        subscription.grace_reminder_notified_at = None
        subscription.access_revoked_at = None
        subscription.access_revoked_notified_at = None
        subscription.provider_expiration_shutdown_at = None
        subscription.provider_expiration_shutdown_error = None
        subscription.provider_expiration_shutdown_alerted_at = None
        if rec_token:
            subscription.provider_rec_token = rec_token
        if repay_url:
            subscription.provider_repay_url = repay_url

    @classmethod
    def _apply_failed_payment(
        cls,
        subscription: Subscription,
        *,
        failure_reason: str | None,
        failed_at: datetime,
        payload: dict[str, Any],
    ) -> None:
        if subscription.status == SubscriptionStatus.ACTIVE:
            subscription.payment_failed_user_notified_at = None
            subscription.grace_reminder_notified_at = None
            subscription.access_revoked_at = None
            subscription.access_revoked_notified_at = None
        subscription.status = SubscriptionStatus.PAST_DUE
        subscription.payment_failed_at = as_utc(failed_at)
        subscription.payment_failure_reason = failure_reason
        cls._update_repay_url(subscription, payload)

    @staticmethod
    def _is_due_for_expiration(subscription: Subscription, cutoff: datetime) -> bool:
        return (
            subscription.status
            in {SubscriptionStatus.ACTIVE, SubscriptionStatus.PAST_DUE}
            and subscription.current_period_end is not None
            and as_utc(subscription.current_period_end) <= cutoff
        )

    async def _record_expiration_shutdown_failure(
        self,
        subscription_id: object,
        reason: str,
        *,
        recurring_status: RecurringStatus = RecurringStatus.CHECK_FAILED,
    ) -> None:
        async with self.session_factory() as session, session.begin():
            subscription = await session.get(Subscription, subscription_id, with_for_update=True)
            if subscription is None:
                return
            subscription.provider_recurring_status = recurring_status.value
            subscription.provider_recurring_checked_at = utc_now()
            subscription.provider_recurring_reason = reason[:1000]
            subscription.provider_expiration_shutdown_at = None
            subscription.provider_expiration_shutdown_error = reason[:1000]

    async def _resume_after_expiration_race(
        self,
        subscription_id: object,
        order_reference: str,
    ) -> None:
        try:
            await self.wayforpay.resume_recurring(order_reference)
        except Exception as error:
            await self._record_expiration_shutdown_failure(
                subscription_id,
                f"{type(error).__name__}: recurring RESUME failed after renewal race",
                recurring_status=RecurringStatus.SUSPENDED,
            )
            return
        async with self.session_factory() as session, session.begin():
            subscription = await session.get(Subscription, subscription_id, with_for_update=True)
            if subscription is None:
                return
            subscription.provider_recurring_status = RecurringStatus.ACTIVE.value
            subscription.provider_recurring_checked_at = utc_now()
            subscription.provider_recurring_reason = (
                "Recurring rule resumed because the entitlement renewed during expiration"
            )
            subscription.provider_expiration_shutdown_at = None
            subscription.provider_expiration_shutdown_error = None
            subscription.provider_expiration_shutdown_alerted_at = None

    @classmethod
    def _update_repay_url(
        cls,
        subscription: Subscription,
        payload: dict[str, Any],
    ) -> None:
        repay_url = cls._repay_url_from_payload(payload)
        if repay_url:
            subscription.provider_repay_url = repay_url

    @classmethod
    async def _restore_repay_url(
        cls,
        session: AsyncSession,
        subscription: Subscription,
    ) -> None:
        if subscription.provider_repay_url is not None:
            return
        payloads = await session.scalars(
            select(Payment.provider_payload)
            .where(
                Payment.subscription_id == subscription.id,
                Payment.status == PaymentStatus.APPROVED,
                Payment.failure_reason.is_(None),
            )
            .order_by(Payment.created_at.desc())
        )
        for payload in payloads:
            repay_url = cls._repay_url_from_payload(payload)
            if repay_url:
                subscription.provider_repay_url = repay_url
                return

    @staticmethod
    def _repay_url_from_payload(payload: dict[str, Any]) -> str | None:
        value = payload.get("repayUrl")
        if not isinstance(value, str) or len(value) > 2048:
            return None
        parsed = urlsplit(value)
        hostname = (parsed.hostname or "").casefold()
        if (
            parsed.scheme.casefold() != "https"
            or not hostname
            or (hostname != "wayforpay.com" and not hostname.endswith(".wayforpay.com"))
        ):
            return None
        return value

    @staticmethod
    def _recurring_result(
        order_reference: str,
        data: dict[str, Any],
    ) -> tuple[RecurringAuditResult, str]:
        try:
            reason_code = int(data.get("reasonCode", 0))
        except (TypeError, ValueError):
            reason_code = 0
        provider_status = str(data.get("status") or "").casefold()
        known_statuses = {
            item.value: item
            for item in (
                RecurringStatus.ACTIVE,
                RecurringStatus.CREATED,
                RecurringStatus.CONFIRMED,
                RecurringStatus.SUSPENDED,
                RecurringStatus.REMOVED,
                RecurringStatus.COMPLETED,
            )
        }
        if reason_code == 4100 and provider_status in known_statuses:
            status = known_statuses[provider_status]
        elif reason_code == 4107 and provider_status == RecurringStatus.REMOVED.value:
            # WayForPay reports a closed regular-payment rule as Removed with
            # reasonCode 4107 instead of the generic successful code 4100.
            status = RecurringStatus.REMOVED
        elif reason_code == 4102:
            status = RecurringStatus.MISSING
        else:
            status = RecurringStatus.CHECK_FAILED
        reason = str(data.get("reason") or "WayForPay returned no reason")
        return RecurringAuditResult(order_reference=order_reference, status=status), reason

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
