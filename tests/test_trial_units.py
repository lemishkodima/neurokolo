from datetime import UTC, datetime
from decimal import Decimal

import pytest

from club_bot.domain.trial import add_trial_period, parse_trial


@pytest.mark.parametrize("text,count,unit", [
    ("1 1 день", 1, "days"), ("1 137 днів", 137, "days"),
    ("1 3 тижні", 3, "weeks"), ("1 5 місяців", 5, "months"),
    ("1 2", 2, "months"),
])
def test_admin_duration_input(text: str, count: int, unit: str) -> None:
    assert parse_trial(text) == (Decimal("1"), count, unit)


@pytest.mark.parametrize("text", ["1 0 днів", "1 -1 тижні", "1 1.5 днів", "1 3 години",
                                  "1 999999999999999999999 днів", "1"])
def test_invalid_duration(text: str) -> None:
    with pytest.raises(ValueError):
        parse_trial(text)


def test_calendar_month_and_week() -> None:
    start = datetime(2028, 1, 31, 12, tzinfo=UTC)
    assert add_trial_period(start, 1, "months") == datetime(2028, 2, 29, 12, tzinfo=UTC)
    assert add_trial_period(start, 1, "weeks") == datetime(2028, 2, 7, 12, tzinfo=UTC)
