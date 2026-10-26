"""Separate built-in and custom summary modes.

Revision ID: 20261020_0015
Revises: 20261020_0014
"""

import sqlalchemy as sa

from alembic import op

revision = "20261020_0015"
down_revision = "20261020_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("summaries") as batch_op:
        batch_op.add_column(sa.Column("mode", sa.String(length=16), nullable=True))

    op.execute("UPDATE summaries SET mode = 'template'")
    op.execute(
        """
        UPDATE summaries
        SET mode = 'custom'
        WHERE EXISTS (
            SELECT 1
            FROM summary_versions
            WHERE summary_versions.summary_id = summaries.id
              AND summary_versions.prompt NOT IN (
                  'Tóm tắt chung nội dung cuộc họp.',
                  'Tóm tắt cuộc họp giao ban.',
                  'Tóm tắt cuộc họp biểu quyết.'
              )
        )
        """
    )

    with op.batch_alter_table("summaries") as batch_op:
        batch_op.alter_column(
            "mode", existing_type=sa.String(length=16), nullable=False
        )
        batch_op.drop_constraint(
            "uq_summary_transcript_template_language", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_summary_transcript_mode_template_language",
            [
                "meeting_id",
                "transcript_version_id",
                "mode",
                "template_type",
                "output_language",
            ],
        )


def downgrade() -> None:
    with op.batch_alter_table("summaries") as batch_op:
        batch_op.drop_constraint(
            "uq_summary_transcript_mode_template_language", type_="unique"
        )
        batch_op.create_unique_constraint(
            "uq_summary_transcript_template_language",
            ["meeting_id", "transcript_version_id", "template_type", "output_language"],
        )
        batch_op.drop_column("mode")
