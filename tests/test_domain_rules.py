from datetime import UTC, datetime
from decimal import Decimal

from club_bot.domain.rules import money_to_string, next_month


def test_next_month_uses_calendar_month() -> None:
    assert next_month(datetime(2026, 1, 31, 12, tzinfo=UTC)) == datetime(
        2026, 2, 28, 12, tzinfo=UTC
    )


def test_money_is_always_two_decimal_places() -> None:
    assert money_to_string(Decimal("990")) == "990.00"
