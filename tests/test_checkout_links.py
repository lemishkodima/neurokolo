from __future__ import annotations

import pytest

from club_bot.services.checkout_links import (
    InvalidPersonalCheckoutToken,
    add_query_parameter,
    create_personal_checkout_token,
    verify_personal_checkout_token,
)


def test_personal_checkout_token_is_signed_and_expires() -> None:
    token = create_personal_checkout_token(501, "internal-secret", now=1_000)

    assert verify_personal_checkout_token(token, "internal-secret", now=1_001) == 501
    with pytest.raises(InvalidPersonalCheckoutToken):
        verify_personal_checkout_token(f"{token[:-1]}x", "internal-secret", now=1_001)
    with pytest.raises(InvalidPersonalCheckoutToken):
        verify_personal_checkout_token(token, "different-secret", now=1_001)
    with pytest.raises(InvalidPersonalCheckoutToken, match="expired"):
        verify_personal_checkout_token(token, "internal-secret", now=1_900)


def test_add_query_parameter_preserves_existing_checkout_url() -> None:
    assert (
        add_query_parameter("https://neurokolo.com/club?referral_code=friend", "owner", "token")
        == "https://neurokolo.com/club?referral_code=friend&owner=token"
    )
