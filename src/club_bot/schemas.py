from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, EmailStr, Field


class CheckoutCreate(BaseModel):
    plan_code: str | None = Field(default=None, min_length=1, max_length=64)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=32)
    referral_code: str | None = Field(default=None, max_length=32)
    return_url: str | None = None


class CheckoutResponse(BaseModel):
    checkout_token: str
    order_reference: str
    bot_claim_url: str
    gateway_url: str
    gateway_fields: dict[str, Any]
    expires_at: datetime


class SubscriptionView(BaseModel):
    plan_name: str
    billing_months: int
    status: str
    current_period_end: datetime | None
    cancel_at_period_end: bool
