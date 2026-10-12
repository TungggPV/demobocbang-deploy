"""Use recording as the initial meeting status.

Revision ID: 20261012_0012
Revises: 20261012_0011
"""

from alembic import op

revision = "20261012_0012"
down_revision = "20261012_0011"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("UPDATE meetings SET status = 'recording' WHERE status = 'created'")


def downgrade() -> None:
    # Existing recordings may already contain transcript data, so a downgrade
    # must not guess which rows were originally in the legacy created state.
    pass
