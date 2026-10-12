"""Add the editable-ready meeting status.

Revision ID: 20261012_0011
Revises: b326ebb08ce4
"""

from alembic import op

revision = "20261012_0011"
down_revision = "b326ebb08ce4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # PostgreSQL persists MeetingStatus as a native enum. SQLite test databases
    # use SQLAlchemy's CHECK-less VARCHAR emulation and need no schema change.
    if op.get_bind().dialect.name == "postgresql":
        # PostgreSQL requires a newly-added enum value to be committed before it
        # can be used by the data backfill below.
        with op.get_context().autocommit_block():
            op.execute(
                "ALTER TYPE meeting_status ADD VALUE IF NOT EXISTS 'ready' AFTER 'processing'"
            )
    op.execute(
        "UPDATE meetings SET status = 'ready' "
        "WHERE source_type = 'upload' AND status = 'completed'"
    )


def downgrade() -> None:
    op.execute(
        "UPDATE meetings SET status = 'completed' "
        "WHERE source_type = 'upload' AND status = 'ready'"
    )
    # PostgreSQL enum values cannot be removed safely without rewriting the
    # meetings table. Keeping the unused value is safe for a code rollback.
