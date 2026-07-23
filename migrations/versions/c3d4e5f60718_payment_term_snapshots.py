"""snapshot checkout and subscription payment terms

Revision ID: c3d4e5f60718
Revises: a7b3c9d14210
Create Date: 2026-07-23 16:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "c3d4e5f60718"
down_revision: str | None = "a7b3c9d14210"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "checkout_sessions",
        sa.Column("amount", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.add_column(
        "checkout_sessions",
        sa.Column("currency", sa.String(length=3), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("billing_amount", sa.Numeric(precision=12, scale=2), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("billing_currency", sa.String(length=3), nullable=True),
    )

    op.execute(
        """
        UPDATE checkout_sessions AS checkout
        SET amount = plan.price, currency = plan.currency
        FROM plans AS plan
        WHERE checkout.plan_id = plan.id
        """
    )
    op.execute(
        """
        UPDATE subscriptions AS subscription
        SET billing_amount = plan.price, billing_currency = plan.currency
        FROM plans AS plan
        WHERE subscription.plan_id = plan.id
        """
    )

    op.alter_column("checkout_sessions", "amount", nullable=False)
    op.alter_column("checkout_sessions", "currency", nullable=False)
    op.alter_column("subscriptions", "billing_amount", nullable=False)
    op.alter_column("subscriptions", "billing_currency", nullable=False)


def downgrade() -> None:
    op.drop_column("subscriptions", "billing_currency")
    op.drop_column("subscriptions", "billing_amount")
    op.drop_column("checkout_sessions", "currency")
    op.drop_column("checkout_sessions", "amount")
