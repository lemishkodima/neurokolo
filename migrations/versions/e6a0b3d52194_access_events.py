"""Persist Telegram moderation provenance independently of subscription transactions."""
import sqlalchemy as sa
from alembic import op

revision = "e6a0b3d52194"
down_revision = "d5f9a2c41083"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "access_events",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("chat_id", sa.BigInteger(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("actor_id", sa.BigInteger(), nullable=True),
        sa.Column("action", sa.String(40), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_access_events_member", "access_events",
                    ["chat_id", "telegram_id", "occurred_at"])


def downgrade() -> None:
    op.drop_table("access_events")
