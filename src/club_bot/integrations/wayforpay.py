from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping, Sequence
from datetime import datetime
from decimal import Decimal
from typing import Any, cast

import httpx

from club_bot.domain.rules import money_to_string


class WayForPayError(RuntimeError):
    pass


class InvalidWayForPaySignature(WayForPayError):
    pass


# Public integration credentials published by WayForPay for test payments.
WAYFORPAY_TEST_MERCHANT_ACCOUNT = "test_merch_n1"
WAYFORPAY_TEST_SECRET_KEY = "flk3409refn54t54t*FNJRET"
WAYFORPAY_TEST_MERCHANT_PASSWORD = "d485396ae413eb60dc251b0899b261c2"


class WayForPayClient:
    def __init__(
        self,
        *,
        merchant_account: str,
        merchant_domain: str,
        secret_key: str,
        merchant_password: str,
        api_url: str,
        checkout_url: str,
        http_client: httpx.AsyncClient,
    ) -> None:
        self.merchant_account = merchant_account
        self.merchant_domain = merchant_domain
        self.secret_key = secret_key
        self.merchant_password = merchant_password
        self.api_url = api_url
        self.checkout_url = checkout_url
        self.http_client = http_client

    def _sign(self, values: Sequence[object]) -> str:
        message = ";".join(str(value) for value in values).encode()
        return hmac.new(self.secret_key.encode(), message, hashlib.md5).hexdigest()

    def build_purchase_payload(
        self,
        *,
        order_reference: str,
        order_date: int,
        amount: Decimal,
        currency: str,
        product_name: str,
        service_url: str,
        return_url: str,
        date_next: datetime,
        email: str | None = None,
        phone: str | None = None,
    ) -> dict[str, Any]:
        amount_text = money_to_string(amount)
        products = [product_name]
        counts = [1]
        prices = [amount_text]
        signature = self._sign(
            [
                self.merchant_account,
                self.merchant_domain,
                order_reference,
                order_date,
                amount_text,
                currency,
                *products,
                *counts,
                *prices,
            ]
        )
        payload: dict[str, Any] = {
            "merchantAccount": self.merchant_account,
            "merchantDomainName": self.merchant_domain,
            "merchantAuthType": "SimpleSignature",
            "merchantTransactionType": "SALE",
            "merchantTransactionSecureType": "AUTO",
            "merchantSignature": signature,
            "apiVersion": 1,
            "orderReference": order_reference,
            "orderDate": order_date,
            "amount": amount_text,
            "currency": currency,
            "productName": products,
            "productCount": counts,
            "productPrice": prices,
            "serviceUrl": service_url,
            "returnUrl": return_url,
            "language": "UA",
            "regularBehavior": "preset",
            "regularMode": "monthly",
            "regularAmount": amount_text,
            "regularOn": 1,
            "dateNext": date_next.strftime("%d.%m.%Y"),
            "defaultPaymentSystem": "card",
        }
        if email:
            payload["clientEmail"] = email
        if phone:
            payload["clientPhone"] = phone
        return payload

    def verify_callback(self, payload: Mapping[str, Any]) -> None:
        required = (
            "merchantAccount",
            "orderReference",
            "amount",
            "currency",
            "authCode",
            "cardPan",
            "transactionStatus",
            "reasonCode",
            "merchantSignature",
        )
        missing = [key for key in required if key not in payload]
        if missing:
            raise InvalidWayForPaySignature(f"Missing callback fields: {', '.join(missing)}")
        if payload["merchantAccount"] != self.merchant_account:
            raise InvalidWayForPaySignature("Unexpected merchant account")
        expected = self._sign([payload[key] for key in required[:-1]])
        if not hmac.compare_digest(expected, str(payload["merchantSignature"])):
            raise InvalidWayForPaySignature("Invalid callback signature")

    def callback_response(self, order_reference: str) -> dict[str, str | int]:
        timestamp = int(time.time())
        status = "accept"
        return {
            "orderReference": order_reference,
            "status": status,
            "time": timestamp,
            "signature": self._sign([order_reference, status, timestamp]),
        }

    async def suspend_recurring(self, order_reference: str) -> None:
        response = await self.http_client.post(
            self.api_url,
            json={
                "requestType": "SUSPEND",
                "merchantAccount": self.merchant_account,
                "merchantPassword": self.merchant_password,
                "orderReference": order_reference,
            },
        )
        response.raise_for_status()
        data = response.json()
        if int(data.get("reasonCode", 0)) != 4100:
            raise WayForPayError(str(data.get("reason", "WayForPay rejected SUSPEND")))

    async def resume_recurring(self, order_reference: str) -> None:
        response = await self.http_client.post(
            self.api_url,
            json={
                "requestType": "RESUME",
                "merchantAccount": self.merchant_account,
                "merchantPassword": self.merchant_password,
                "orderReference": order_reference,
            },
        )
        response.raise_for_status()
        data = response.json()
        if int(data.get("reasonCode", 0)) != 4100:
            raise WayForPayError(str(data.get("reason", "WayForPay rejected RESUME")))

    async def recurring_status(self, order_reference: str) -> dict[str, Any]:
        response = await self.http_client.post(
            self.api_url,
            json={
                "requestType": "STATUS",
                "merchantAccount": self.merchant_account,
                "merchantPassword": self.merchant_password,
                "orderReference": order_reference,
            },
        )
        response.raise_for_status()
        return cast(dict[str, Any], response.json())
