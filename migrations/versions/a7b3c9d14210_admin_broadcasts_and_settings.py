"""admin broadcasts and settings

Revision ID: a7b3c9d14210
Revises: f42a4f962826
Create Date: 2026-07-22 18:00:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "a7b3c9d14210"
down_revision: str | None = "f42a4f962826"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "admins",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("telegram_id", sa.BigInteger(), nullable=False),
        sa.Column("added_by_telegram_id", sa.BigInteger(), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_admins_telegram_id", "admins", ["telegram_id"], unique=True)
    op.create_index("ix_admins_is_active", "admins", ["is_active"], unique=False)

    op.create_table(
        "app_settings",
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("key"),
    )

    op.create_table(
        "broadcasts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "DRAFT",
                "QUEUED",
                "SENDING",
                "COMPLETED",
                "FAILED",
                name="broadcaststatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column(
            "target",
            sa.Enum(
                "ALL_USERS",
                "ACTIVE_SUBSCRIBERS",
                name="broadcasttarget",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("source_chat_id", sa.BigInteger(), nullable=False),
        sa.Column("source_message_ids", sa.JSON(), nullable=False),
        sa.Column("buttons", sa.JSON(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("total_recipients", sa.Integer(), nullable=False),
        sa.Column("sent_count", sa.Integer(), nullable=False),
        sa.Column("failed_count", sa.Integer(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_broadcasts_created_by_telegram_id",
        "broadcasts",
        ["created_by_telegram_id"],
        unique=False,
    )
    op.create_index("ix_broadcasts_scheduled_at", "broadcasts", ["scheduled_at"], unique=False)
    op.create_index("ix_broadcasts_queue", "broadcasts", ["status", "scheduled_at"], unique=False)

    op.create_table(
        "broadcast_recipients",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("broadcast_id", sa.Uuid(), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
        sa.Column(
            "status",
            sa.Enum(
                "PENDING",
                "SENT",
                "FAILED",
                name="deliverystatus",
                native_enum=False,
            ),
            nullable=False,
        ),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["broadcast_id"], ["broadcasts.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("broadcast_id", "user_id"),
    )
    op.create_index(
        "ix_broadcast_recipients_broadcast_id",
        "broadcast_recipients",
        ["broadcast_id"],
        unique=False,
    )
    op.create_index(
        "ix_broadcast_recipients_user_id",
        "broadcast_recipients",
        ["user_id"],
        unique=False,
    )
    op.create_index(
        "ix_broadcast_recipients_pending",
        "broadcast_recipients",
        ["broadcast_id", "status"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_broadcast_recipients_pending", table_name="broadcast_recipients")
    op.drop_index("ix_broadcast_recipients_user_id", table_name="broadcast_recipients")
    op.drop_index("ix_broadcast_recipients_broadcast_id", table_name="broadcast_recipients")
    op.drop_table("broadcast_recipients")
    op.drop_index("ix_broadcasts_queue", table_name="broadcasts")
    op.drop_index("ix_broadcasts_scheduled_at", table_name="broadcasts")
    op.drop_index("ix_broadcasts_created_by_telegram_id", table_name="broadcasts")
    op.drop_table("broadcasts")
    op.drop_table("app_settings")
    op.drop_index("ix_admins_is_active", table_name="admins")
    op.drop_index("ix_admins_telegram_id", table_name="admins")
    op.drop_table("admins")
