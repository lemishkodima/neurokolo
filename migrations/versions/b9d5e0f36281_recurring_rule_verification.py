"""add recurring rule verification state

Revision ID: b9d5e0f36281
Revises: a8c4d9e25170
Create Date: 2026-07-24 15:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "b9d5e0f36281"
down_revision: str | None = "a8c4d9e25170"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column("provider_recurring_status", sa.String(length=32), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "provider_recurring_checked_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column("provider_recurring_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "provider_recurring_alerted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_subscriptions_provider_recurring_status",
        "subscriptions",
        ["provider_recurring_status"],
        unique=False,
    )
    op.execute(
        """
        UPDATE subscriptions
        SET provider_recurring_status = CASE
            WHEN provider = 'wayforpay'
                 AND status IN ('ACTIVE', 'PAST_DUE')
                 AND cancel_at_period_end = false
                THEN 'pending'
            ELSE 'not_applicable'
        END
        """
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscriptions_provider_recurring_status",
        table_name="subscriptions",
    )
    op.drop_column("subscriptions", "provider_recurring_alerted_at")
    op.drop_column("subscriptions", "provider_recurring_reason")
    op.drop_column("subscriptions", "provider_recurring_checked_at")
    op.drop_column("subscriptions", "provider_recurring_status")
