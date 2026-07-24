from __future__ import annotations

from datetime import timedelta
from pathlib import Path
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from club_bot.db import create_engine, create_session_factory
from club_bot.domain.enums import PaymentStatus, SubscriptionStatus
from club_bot.domain.rules import utc_now
from club_bot.models import Base, Payment, Plan, Subscription, User
from club_bot.services.admin import AdminService
from club_bot.services.subscription_notifications import SubscriptionNotificationService


class FakeBot:
    def __init__(self) -> None:
        self.messages: list[tuple[int, str, Any]] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        reply_markup: Any,
    ) -> None:
        self.messages.append((chat_id, text, reply_markup))


class FakeAccessService:
    pass


class FakeSettingsService:
    pass


async def test_failed_payment_reminder_admin_alert_and_final_notice(
    tmp_path: Path,
) -> None:
    engine = create_engine(f"sqlite+aiosqlite:///{tmp_path / 'dunning.db'}")
    session_factory: async_sessionmaker[AsyncSession] = create_session_factory(engine)
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)

    now = utc_now()
    async with session_factory() as session, session.begin():
        user = User(
            telegram_id=123,
            username="member",
            first_name="Member",
            referral_code="MEMBER123",
        )
        plan = Plan(code="base", name="Base", price=990, currency="UAH")
        session.add_all([user, plan])
        await session.flush()
        subscription = Subscription(
            user_id=user.id,
            plan_id=plan.id,
            status=SubscriptionStatus.PAST_DUE,
            current_period_start=now - timedelta(days=30),
            current_period_end=now - timedelta(hours=23),
            billing_amount=990,
            billing_currency="UAH",
            billing_months=1,
            provider="wayforpay",
            provider_subscription_id="CLUB-DUNNING",
            provider_repay_url="https://secure.wayforpay.com/repay/safe-token",
            payment_failed_at=now,
            payment_failure_reason="Not enough funds",
        )
        session.add(subscription)
        await session.flush()
        subscription_id = subscription.id
        payment = Payment(
            subscription_id=subscription.id,
            provider_event_id="failed-event",
            order_reference="CLUB-DUNNING",
            amount=990,
            currency="UAH",
            status=PaymentStatus.DECLINED,
            failure_reason="Not enough funds",
            provider_payload={},
        )
        session.add(payment)
        await session.flush()
        payment_id = payment.id

    bot: Any = FakeBot()
    service = SubscriptionNotificationService(
        bot,
        FakeAccessService(),  # type: ignore[arg-type]
        FakeSettingsService(),  # type: ignore[arg-type]
        session_factory,
        AdminService(session_factory, [900]),
        grace_period_hours=24,
        reminder_hours_before=2,
    )

    assert await service.send_payment_failed("CLUB-DUNNING") is True
    assert [message[0] for message in bot.messages] == [123, 900]
    assert "Не вдалося продовжити підписку" in bot.messages[0][1]
    retry_markup = bot.messages[0][2]
    assert retry_markup.inline_keyboard[0][0].text == "Повторити оплату 💳"
    assert (
        retry_markup.inline_keyboard[0][0].url
        == "https://secure.wayforpay.com/repay/safe-token"
    )
    assert "Невдалий production-платіж" in bot.messages[1][1]

    assert await service.send_payment_failed("CLUB-DUNNING") is False
    assert len(bot.messages) == 2

    assert await service.send_due_grace_reminders() == 1
    assert "Доступ незабаром буде припинено" in bot.messages[2][1]
    assert await service.send_due_grace_reminders() == 0

    async with session_factory() as session, session.begin():
        stored = await session.get(Subscription, subscription_id, with_for_update=True)
        assert stored is not None
        stored.status = SubscriptionStatus.EXPIRED
        stored.access_revoked_at = utc_now()

    assert await service.send_access_revoked_notifications() == 1
    assert "Підписку завершено" in bot.messages[3][1]
    assert await service.send_access_revoked_notifications() == 0

    async with session_factory() as session:
        stored = await session.get(Subscription, subscription_id)
        stored_payment = await session.get(Payment, payment_id)
        assert stored is not None
        assert stored_payment is not None
        assert stored.payment_failed_user_notified_at is not None
        assert stored_payment.admin_notified_at is not None
        assert stored.grace_reminder_notified_at is not None
        assert stored.access_revoked_notified_at is not None

    await engine.dispose()
