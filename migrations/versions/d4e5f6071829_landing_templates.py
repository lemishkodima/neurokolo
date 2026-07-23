"""add editable HTML landing templates

Revision ID: d4e5f6071829
Revises: c3d4e5f60718
Create Date: 2026-07-23 18:10:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "d4e5f6071829"
down_revision: str | None = "c3d4e5f60718"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "landing_templates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("slug", sa.String(length=64), nullable=False),
        sa.Column("landing_title", sa.String(length=255), nullable=False),
        sa.Column("channel_title", sa.String(length=255), nullable=False),
        sa.Column("landing_description", sa.Text(), nullable=False),
        sa.Column("html_template", sa.Text(), nullable=False),
        sa.Column("download_url", sa.String(length=2048), nullable=False),
        sa.Column("created_by_telegram_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_landing_templates_created_by_telegram_id"),
        "landing_templates",
        ["created_by_telegram_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_landing_templates_slug"),
        "landing_templates",
        ["slug"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_landing_templates_slug"), table_name="landing_templates")
    op.drop_index(
        op.f("ix_landing_templates_created_by_telegram_id"),
        table_name="landing_templates",
    )
    op.drop_table("landing_templates")
