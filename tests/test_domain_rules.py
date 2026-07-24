from datetime import UTC, datetime
from decimal import Decimal

import pytest

from club_bot.domain.billing import (
    add_billing_months,
    billing_period_label,
    wayforpay_regular_mode,
)
from club_bot.domain.rules import money_to_string, next_month


def test_next_month_uses_calendar_month() -> None:
    assert next_month(datetime(2026, 1, 31, 12, tzinfo=UTC)) == datetime(
        2026, 2, 28, 12, tzinfo=UTC
    )


def test_money_is_always_two_decimal_places() -> None:
    assert money_to_string(Decimal("990")) == "990.00"


@pytest.mark.parametrize(
    ("months", "mode", "label"),
    [
        (1, "monthly", "1 місяць"),
        (2, "bimonthly", "2 місяці"),
        (3, "quarterly", "3 місяці"),
        (6, "halfyearly", "6 місяців"),
        (12, "yearly", "12 місяців"),
    ],
)
def test_supported_billing_periods_match_wayforpay(
    months: int,
    mode: str,
    label: str,
) -> None:
    started_at = datetime(2026, 1, 31, 12, tzinfo=UTC)

    assert wayforpay_regular_mode(months) == mode
    assert billing_period_label(months) == label
    assert add_billing_months(started_at, months) > started_at


def test_unsupported_billing_period_is_rejected() -> None:
    with pytest.raises(ValueError):
        wayforpay_regular_mode(4)
