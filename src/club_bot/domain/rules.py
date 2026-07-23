from __future__ import annotations

import secrets
from datetime import UTC, datetime
from decimal import Decimal

from dateutil.relativedelta import relativedelta


def next_month(value: datetime) -> datetime:
    """Advance by one calendar month while preserving timezone information."""
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value + relativedelta(months=1)


def as_utc(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def utc_now() -> datetime:
    return datetime.now(UTC)


def generate_public_token() -> str:
    # 32 URL-safe characters, valid for Telegram's 64-character start payload.
    return secrets.token_urlsafe(24)


def generate_referral_code() -> str:
    return secrets.token_urlsafe(8).replace("-", "A").replace("_", "B")


def money_to_string(value: Decimal) -> str:
    normalized = value.quantize(Decimal("0.01"))
    return format(normalized, "f")
