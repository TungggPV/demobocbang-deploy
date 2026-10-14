"""Add avatar_url to users table.

Revision ID: 20261014_0014
Revises: 20261012_0013
"""

from alembic import op
import sqlalchemy as sa

revision = "20261014_0014"
down_revision = "20261012_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.add_column(sa.Column("avatar_url", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("users") as batch_op:
        batch_op.drop_column("avatar_url")
