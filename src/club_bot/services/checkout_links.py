from __future__ import annotations

import base64
import hashlib
import hmac
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

PERSONAL_CHECKOUT_LINK_TTL_SECONDS = 15 * 60


class InvalidPersonalCheckoutToken(ValueError):
    pass


def create_personal_checkout_token(
    telegram_id: int,
    secret: str,
    *,
    now: int | None = None,
    ttl_seconds: int = PERSONAL_CHECKOUT_LINK_TTL_SECONDS,
) -> str:
    if telegram_id <= 0:
        raise ValueError("Telegram ID must be positive")
    issued_at = int(time.time()) if now is None else now
    payload = f"v1:{telegram_id}:{issued_at + ttl_seconds}".encode()
    signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    return f"{_encode(payload)}.{_encode(signature)}"


def verify_personal_checkout_token(
    token: str,
    secret: str,
    *,
    now: int | None = None,
) -> int:
    try:
        encoded_payload, encoded_signature = token.split(".", maxsplit=1)
        payload = _decode(encoded_payload)
        signature = _decode(encoded_signature)
    except (ValueError, UnicodeError) as error:
        raise InvalidPersonalCheckoutToken("Malformed personal checkout token") from error

    expected_signature = hmac.new(secret.encode(), payload, hashlib.sha256).digest()
    if not hmac.compare_digest(expected_signature, signature):
        raise InvalidPersonalCheckoutToken("Invalid personal checkout signature")

    try:
        version, raw_telegram_id, raw_expires_at = payload.decode().split(":")
        telegram_id = int(raw_telegram_id)
        expires_at = int(raw_expires_at)
    except (ValueError, UnicodeError) as error:
        raise InvalidPersonalCheckoutToken("Malformed personal checkout payload") from error
    if version != "v1" or telegram_id <= 0:
        raise InvalidPersonalCheckoutToken("Unsupported personal checkout payload")
    current_time = int(time.time()) if now is None else now
    if expires_at <= current_time:
        raise InvalidPersonalCheckoutToken("Personal checkout link has expired")
    return telegram_id


def add_query_parameter(url: str, name: str, value: str) -> str:
    parts = urlsplit(url)
    query = parse_qsl(parts.query, keep_blank_values=True)
    query.append((name, value))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment))


def _encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")


def _decode(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    return base64.b64decode(value + padding, altchars=b"-_", validate=True)
