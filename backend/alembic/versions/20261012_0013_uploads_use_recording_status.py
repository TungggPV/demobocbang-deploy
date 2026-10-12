"""Use recording for editable uploaded meetings.

Revision ID: 20261012_0013
Revises: 20261012_0012
"""

from alembic import op

revision = "20261012_0013"
down_revision = "20261012_0012"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        "UPDATE meetings SET status = 'recording' "
        "WHERE source_type = 'upload' AND status = 'ready'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE meetings SET status = 'ready' "
        "WHERE source_type = 'upload' AND status = 'recording'"
    )
