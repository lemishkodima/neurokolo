"""Add nullable duration snapshots; preserve all existing terms."""
import sqlalchemy as sa
from alembic import op

revision = "a8c2d5f74316"
down_revision = "f7b1c4e63205"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for table, prefix in (("plans", "trial"), ("checkout_sessions", "initial")):
        op.add_column(table, sa.Column(f"{prefix}_duration", sa.Integer(), nullable=True))
        op.add_column(table, sa.Column(f"{prefix}_unit", sa.String(10), nullable=True))


def downgrade() -> None:
    for table, prefix in (("checkout_sessions", "initial"), ("plans", "trial")):
        op.drop_column(table, f"{prefix}_unit")
        op.drop_column(table, f"{prefix}_duration")
