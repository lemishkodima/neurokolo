"""normalize subscription button label

Revision ID: c4e8d1b30972
Revises: b9d5e0f36281
Create Date: 2026-07-24 15:40:00
"""

from collections.abc import Sequence

from alembic import op

revision: str = "c4e8d1b30972"
down_revision: str | None = "b9d5e0f36281"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute(
        """
        UPDATE app_settings
        SET value = 'Моя підписка'
        WHERE key = 'button_subscription'
        """
    )


def downgrade() -> None:
    pass
