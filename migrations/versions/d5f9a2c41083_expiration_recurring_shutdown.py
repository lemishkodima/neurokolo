"""track recurring shutdown before access expiration

Revision ID: d5f9a2c41083
Revises: c4e8d1b30972
Create Date: 2026-10-06 19:30:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d5f9a2c41083"
down_revision: str | None = "c4e8d1b30972"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "subscriptions",
        sa.Column(
            "provider_expiration_shutdown_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.add_column(
        "subscriptions",
        sa.Column("provider_expiration_shutdown_error", sa.Text(), nullable=True),
    )
    op.add_column(
        "subscriptions",
        sa.Column(
            "provider_expiration_shutdown_alerted_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_subscriptions_provider_expiration_shutdown_at",
        "subscriptions",
        ["provider_expiration_shutdown_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_subscriptions_provider_expiration_shutdown_at",
        table_name="subscriptions",
    )
    op.drop_column("subscriptions", "provider_expiration_shutdown_alerted_at")
    op.drop_column("subscriptions", "provider_expiration_shutdown_error")
    op.drop_column("subscriptions", "provider_expiration_shutdown_at")
