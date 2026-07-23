from datetime import UTC, datetime
from decimal import Decimal

import httpx
import pytest

from club_bot.integrations.wayforpay import (
    InvalidWayForPaySignature,
    WayForPayClient,
)


@pytest.fixture
def client() -> WayForPayClient:
    return WayForPayClient(
        merchant_account="merchant",
        merchant_domain="example.com",
        secret_key="secret",
        merchant_password="password",
        api_url="https://api.example.test/regularApi",
        checkout_url="https://secure.example.test/pay",
        http_client=httpx.AsyncClient(),
    )


def test_purchase_payload_is_signed_and_recurring(client: WayForPayClient) -> None:
    payload = client.build_purchase_payload(
        order_reference="CLUB-1",
        order_date=1_700_000_000,
        amount=Decimal("990"),
        currency="UAH",
        product_name="Club Base",
        service_url="https://bot.example.com/webhooks/wayforpay",
        return_url="https://example.com/complete",
        date_next=datetime(2026, 8, 22, tzinfo=UTC),
    )

    assert payload["regularMode"] == "monthly"
    assert payload["regularBehavior"] == "preset"
    assert payload["regularOn"] == 1
    assert payload["dateNext"] == "22.08.2026"
    assert payload["merchantSignature"] == client._sign(
        [
            "merchant",
            "example.com",
            "CLUB-1",
            1_700_000_000,
            "990.00",
            "UAH",
            "Club Base",
            1,
            "990.00",
        ]
    )


def test_callback_signature_is_verified(client: WayForPayClient) -> None:
    payload = {
        "merchantAccount": "merchant",
        "orderReference": "CLUB-1",
        "amount": "990.00",
        "currency": "UAH",
        "authCode": "123456",
        "cardPan": "42****42",
        "transactionStatus": "Approved",
        "reasonCode": 1100,
    }
    payload["merchantSignature"] = client._sign(list(payload.values()))
    client.verify_callback(payload)

    payload["merchantSignature"] = "invalid"
    with pytest.raises(InvalidWayForPaySignature):
        client.verify_callback(payload)
