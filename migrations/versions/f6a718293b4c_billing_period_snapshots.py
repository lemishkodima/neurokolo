"""snapshot checkout and subscription billing periods

Revision ID: f6a718293b4c
Revises: e5f60718293a
Create Date: 2026-07-24 10:45:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "f6a718293b4c"
down_revision: str | None = "e5f60718293a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "checkout_sessions",
        sa.Column("billing_months", sa.Integer(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("billing_months", sa.Integer(), nullable=True),
    )

    op.execute(
        """
        UPDATE checkout_sessions AS checkout
        SET billing_months = plan.billing_months
        FROM plans AS plan
        WHERE checkout.plan_id = plan.id
        """
    )
    op.execute(
        """
        UPDATE subscriptions AS subscription
        SET billing_months = plan.billing_months
        FROM plans AS plan
        WHERE subscription.plan_id = plan.id
        """
    )

    op.alter_column("checkout_sessions", "billing_months", nullable=False)
    op.alter_column("subscriptions", "billing_months", nullable=False)


def downgrade() -> None:
    op.drop_column("subscriptions", "billing_months")
    op.drop_column("checkout_sessions", "billing_months")
