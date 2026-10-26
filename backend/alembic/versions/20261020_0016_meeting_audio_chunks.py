"""Retain realtime meeting audio for confirmed voice learning.

Revision ID: 20261020_0016
Revises: 20261020_0015
"""

import sqlalchemy as sa

from alembic import op

revision = "20261020_0016"
down_revision = "20261020_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meeting_audio_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "meeting_id",
            sa.Uuid(),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("recording_id", sa.Uuid(), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=False),
        sa.Column("storage_key", sa.String(512), nullable=False, unique=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.CheckConstraint("end_ms > start_ms", name="ck_audio_chunk_interval"),
    )
    op.create_index("ix_meeting_audio_chunks_meeting_id", "meeting_audio_chunks", ["meeting_id"])


def downgrade() -> None:
    op.drop_table("meeting_audio_chunks")
