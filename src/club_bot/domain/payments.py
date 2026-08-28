from __future__ import annotations

WAYFORPAY_RECURRING_REFERENCE_MARKER = "_WFPREG-"


def canonical_wayforpay_order_reference(order_reference: str) -> str:
    """Return the original rule reference for a WayForPay recurring callback."""
    base_reference, marker, _suffix = order_reference.partition(
        WAYFORPAY_RECURRING_REFERENCE_MARKER
    )
    return base_reference if marker and base_reference else order_reference
