from datetime import UTC, datetime
from decimal import Decimal
from types import SimpleNamespace

from club_bot.bot.routers import subscription_status
from club_bot.schemas import SubscriptionView


async def test_subscription_menu_describes_all_current_tariffs() -> None:
    subscriptions = [
        SubscriptionView(
            plan_name="Річний",
            billing_amount=Decimal("6100"),
            billing_currency="UAH",
            billing_months=12,
            status="active",
            current_period_end=datetime(2027, 7, 24, tzinfo=UTC),
            cancel_at_period_end=False,
            auto_renew_enabled=True,
            provider_recurring_status="active",
        ),
        SubscriptionView(
            plan_name="Місячний",
            billing_amount=Decimal("610"),
            billing_currency="UAH",
            billing_months=1,
            status="past_due",
            current_period_end=datetime(2026, 8, 24, tzinfo=UTC),
            cancel_at_period_end=False,
            auto_renew_enabled=False,
            provider_recurring_status="missing",
        ),
    ]

    class FakeSubscriptionService:
        async def current_subscriptions_for_telegram_user(
            self,
            telegram_id: int,
        ) -> list[SubscriptionView]:
            assert telegram_id == 501
            return subscriptions

    class FakeSettingsService:
        async def menu_content(self, action: str) -> None:
            assert action == "subscription"
            return None

    class FakeMessage:
        def __init__(self) -> None:
            self.from_user = SimpleNamespace(id=501)
            self.answers: list[str] = []

        async def answer(self, text: str) -> None:
            self.answers.append(text)

    message = FakeMessage()
    await subscription_status(
        message,  # type: ignore[arg-type]
        FakeSubscriptionService(),  # type: ignore[arg-type]
        FakeSettingsService(),  # type: ignore[arg-type]
        SimpleNamespace(),  # type: ignore[arg-type]
    )

    assert len(message.answers) == 1
    text = message.answers[0]
    assert "<b>Підписка 1</b>" in text
    assert "<b>Тариф:</b> Річний" in text
    assert "<b>Вартість:</b> 6100.00 UAH" in text
    assert "<b>Період:</b> 12 місяців" in text
    assert "<b>Підписка 2</b>" in text
    assert "<b>Тариф:</b> Місячний" in text
    assert "<b>Статус:</b> очікує повторної оплати" in text
    assert "<b>Статус WayForPay:</b> регулярний платіж не створено" in text
