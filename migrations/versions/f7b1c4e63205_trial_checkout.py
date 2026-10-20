"""Snapshot trial price and initial period separately from recurring terms."""

import sqlalchemy as sa
from alembic import op

revision = "f7b1c4e63205"
down_revision = "e6a0b3d52194"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("plans", sa.Column("trial_price", sa.Numeric(12, 2), nullable=True))
    op.add_column("plans", sa.Column("trial_months", sa.Integer(),
                                   nullable=False, server_default="1"))
    op.add_column("checkout_sessions", sa.Column("recurring_amount", sa.Numeric(12, 2),
                                                nullable=True))
    op.add_column("checkout_sessions", sa.Column("initial_months", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("checkout_sessions", "initial_months")
    op.drop_column("checkout_sessions", "recurring_amount")
    op.drop_column("plans", "trial_months")
    op.drop_column("plans", "trial_price")
