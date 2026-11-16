"""First-period durations, independent from the recurring billing frequency."""
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from dateutil.relativedelta import relativedelta

UNITS = {
    "days": "days", "день": "days", "дні": "days", "днів": "days",
    "weeks": "weeks", "тиждень": "weeks", "тижні": "weeks", "тижнів": "weeks",
    "months": "months", "місяць": "months", "місяці": "months", "місяців": "months",
}


def add_trial_period(start: datetime, count: int, unit: str) -> datetime:
    if unit not in {"days", "weeks", "months"} or count < 1:
        raise ValueError("Тривалість має бути цілим числом від 1: днів, тижнів або місяців.")
    try:
        if unit == "months":
            return start + relativedelta(months=count)
        return start + timedelta(days=count * (7 if unit == "weeks" else 1))
    except (OverflowError, ValueError) as error:
        raise ValueError("Тривалість виходить за межі підтримуваних календарних дат.") from error


def trial_label(count: int, unit: str) -> str:
    return f"{count} " + {"days": "дн.", "weeks": "тиж.", "months": "міс."}[unit]


def parse_trial(text: str) -> tuple[Decimal | None, int, str]:
    if text.strip().casefold() in {"вимкнути", "off"}:
        return None, 1, "months"
    parts = text.replace(",", ".").split()
    if len(parts) not in (2, 3):
        raise ValueError("Приклад: 1 7 днів")
    unit = "months" if len(parts) == 2 else UNITS.get(parts[2].casefold(), "")
    count = int(parts[1])
    add_trial_period(datetime.now(UTC), count, unit)
    return Decimal(parts[0]), count, unit
