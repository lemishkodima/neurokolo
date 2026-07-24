"""add subscription dunning notification state

Revision ID: a8c4d9e25170
Revises: f6a718293b4c
Create Date: 2026-07-24 12:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a8c4d9e25170"
down_revision: str | None = "f6a718293b4c"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("subscriptions", sa.Column("provider_repay_url", sa.Text(), nullable=True))
    op.add_column(
        "subscriptions",
        sa.Column("payment_failed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("payment_failure_reason", sa.Text(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "payment_failed_user_notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column("grace_reminder_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column("access_revoked_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "access_revoked_notified_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "payments",
        sa.Column("admin_notified_at", sa.DateTime(timezone=True), nullable=True),
    )
    # Do not replay historical failures as fresh production alerts after deployment.
    op.execute(
        """
        UPDATE payments
        SET admin_notified_at = created_at
        WHERE status = 'DECLINED'
        """
    )


def downgrade() -> None:
    op.drop_column("payments", "admin_notified_at")
    op.drop_column("subscriptions", "access_revoked_notified_at")
    op.drop_column("subscriptions", "access_revoked_at")
    op.drop_column("subscriptions", "grace_reminder_notified_at")
    op.drop_column("subscriptions", "payment_failed_user_notified_at")
    op.drop_column("subscriptions", "payment_failure_reason")
    op.drop_column("subscriptions", "payment_failed_at")
    op.drop_column("subscriptions", "provider_repay_url")
