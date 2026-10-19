"""Add storage_blobs table for durable database media storage.

Revision ID: 20261019_0015
Revises: 20261014_0014
"""

from alembic import op
import sqlalchemy as sa

revision = "20261019_0015"
down_revision = "20261014_0014"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS storage_blobs (
            storage_key VARCHAR(1024) PRIMARY KEY,
            data BYTEA NOT NULL,
            size_bytes BIGINT NOT NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL,
            updated_at TIMESTAMP WITH TIME ZONE DEFAULT NOW() NOT NULL
        )
        """
    )


def downgrade() -> None:
    op.execute("DROP TABLE IF EXISTS storage_blobs")
