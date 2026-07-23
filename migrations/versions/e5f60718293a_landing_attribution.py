"""track Telegram starts from HTML landing pages

Revision ID: e5f60718293a
Revises: d4e5f6071829
Create Date: 2026-07-23 20:05:00
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "e5f60718293a"
down_revision: str | None = "d4e5f6071829"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "landing_visits",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("landing_template_id", sa.Uuid(), nullable=True),
        sa.Column("landing_slug", sa.String(length=64), nullable=False),
        sa.Column("user_id", sa.Uuid(), nullable=False),
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
        sa.ForeignKeyConstraint(
            ["landing_template_id"],
            ["landing_templates.id"],
            ondelete="SET NULL",
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_landing_visits_landing_slug"),
        "landing_visits",
        ["landing_slug"],
        unique=False,
    )
    op.create_index(
        op.f("ix_landing_visits_landing_template_id"),
        "landing_visits",
        ["landing_template_id"],
        unique=False,
    )
    op.create_index(
        "ix_landing_visits_template_created",
        "landing_visits",
        ["landing_template_id", "created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_landing_visits_user_id"),
        "landing_visits",
        ["user_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_landing_visits_user_id"), table_name="landing_visits")
    op.drop_index("ix_landing_visits_template_created", table_name="landing_visits")
    op.drop_index(
        op.f("ix_landing_visits_landing_template_id"),
        table_name="landing_visits",
    )
    op.drop_index(op.f("ix_landing_visits_landing_slug"), table_name="landing_visits")
    op.drop_table("landing_visits")
