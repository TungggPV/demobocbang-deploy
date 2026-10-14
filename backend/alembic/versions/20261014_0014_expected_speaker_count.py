"""Add an optional expected speaker count for diarization.

Revision ID: 20261014_0014
Revises: 20261012_0013
"""

import sqlalchemy as sa

from alembic import op


revision = "20261014_0014"
down_revision = "20261012_0013"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "meetings",
        sa.Column("expected_speaker_count", sa.Integer(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("meetings", "expected_speaker_count")
