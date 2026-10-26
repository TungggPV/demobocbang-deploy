"""Store the user prompt for every summary version.

Revision ID: 20261020_0014
Revises: 20261019_0015
"""

import sqlalchemy as sa

from alembic import op

revision = "20261020_0014"
down_revision = "20261019_0015"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("summary_versions") as batch_op:
        batch_op.add_column(sa.Column("prompt", sa.Text(), nullable=True))

    op.execute(
        """
        UPDATE summary_versions
        SET prompt = COALESCE(
            (
                SELECT CASE summaries.template_type
                    WHEN 'briefing' THEN 'Tóm tắt cuộc họp giao ban.'
                    WHEN 'voting' THEN 'Tóm tắt cuộc họp biểu quyết.'
                    ELSE 'Tóm tắt chung nội dung cuộc họp.'
                END
                FROM summaries
                WHERE summaries.id = summary_versions.summary_id
            ),
            'Tóm tắt nội dung cuộc họp.'
        )
        """
    )

    with op.batch_alter_table("summary_versions") as batch_op:
        batch_op.alter_column("prompt", existing_type=sa.Text(), nullable=False)


def downgrade() -> None:
    with op.batch_alter_table("summary_versions") as batch_op:
        batch_op.drop_column("prompt")
