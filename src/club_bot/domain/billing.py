from __future__ import annotations

from datetime import UTC, datetime

from dateutil.relativedelta import relativedelta

SUPPORTED_BILLING_MONTHS = (1, 2, 3, 6, 12)

_WAYFORPAY_REGULAR_MODES = {
    1: "monthly",
    2: "bimonthly",
    3: "quarterly",
    6: "halfyearly",
    12: "yearly",
}


def validate_billing_months(value: int) -> int:
    if value not in SUPPORTED_BILLING_MONTHS:
        supported = ", ".join(str(months) for months in SUPPORTED_BILLING_MONTHS)
        raise ValueError(f"Billing period must be one of: {supported} months")
    return value


def add_billing_months(value: datetime, months: int) -> datetime:
    validate_billing_months(months)
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value + relativedelta(months=months)


def wayforpay_regular_mode(months: int) -> str:
    validate_billing_months(months)
    return _WAYFORPAY_REGULAR_MODES[months]


def billing_period_label(months: int) -> str:
    if months == 1:
        suffix = "місяць"
    elif months in (2, 3, 4):
        suffix = "місяці"
    else:
        suffix = "місяців"
    return f"{months} {suffix}"
