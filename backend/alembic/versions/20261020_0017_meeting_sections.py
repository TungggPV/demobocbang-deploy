"""Named meeting sessions and independently versioned per-session summaries."""

from uuid import uuid4

import sqlalchemy as sa

from alembic import op

revision = "20261020_0017"
down_revision = "20261020_0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "meeting_sections",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "meeting_id",
            sa.Uuid(),
            sa.ForeignKey("meetings.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("start_ms", sa.BigInteger(), nullable=False),
        sa.Column("end_ms", sa.BigInteger(), nullable=True),
        sa.Column("is_started", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("meeting_id", "sequence", name="uq_meeting_section_sequence"),
        sa.CheckConstraint(
            "start_ms >= 0 AND (end_ms IS NULL OR end_ms >= start_ms)",
            name="ck_meeting_section_interval",
        ),
    )
    op.create_index("ix_meeting_sections_meeting_id", "meeting_sections", ["meeting_id"])
    bind = op.get_bind()
    meetings = sa.table(
        "meetings",
        sa.column("id", sa.Uuid()),
        sa.column("status", sa.String()),
        sa.column("duration_ms", sa.BigInteger()),
        sa.column("current_transcript_version_id", sa.Uuid()),
    )
    sections = sa.table(
        "meeting_sections",
        sa.column("id", sa.Uuid()),
        sa.column("meeting_id", sa.Uuid()),
        sa.column("sequence", sa.Integer()),
        sa.column("title", sa.String()),
        sa.column("start_ms", sa.BigInteger()),
        sa.column("end_ms", sa.BigInteger()),
        sa.column("is_started", sa.Boolean()),
    )
    transcript = sa.table(
        "transcripts", sa.column("id", sa.Uuid()), sa.column("meeting_id", sa.Uuid())
    )
    version = sa.table(
        "transcript_versions", sa.column("id", sa.Uuid()), sa.column("transcript_id", sa.Uuid())
    )
    segment = sa.table(
        "segment_versions",
        sa.column("transcript_version_id", sa.Uuid()),
        sa.column("end_ms", sa.BigInteger()),
    )
    for meeting in bind.execute(sa.select(meetings)).mappings():
        legacy_end = (
            bind.execute(
                sa.select(sa.func.max(segment.c.end_ms))
                .select_from(
                    segment.join(version, segment.c.transcript_version_id == version.c.id).join(
                        transcript,
                        version.c.transcript_id == transcript.c.id,
                    )
                )
                .where(transcript.c.meeting_id == meeting["id"])
            ).scalar()
            or 0
        )
        bind.execute(
            sections.insert().values(
                id=uuid4(),
                meeting_id=meeting["id"],
                sequence=1,
                title="Phiên 1",
                start_ms=0,
                end_ms=max(meeting["duration_ms"] or 0, legacy_end)
                if meeting["status"] == "completed"
                else None,
                is_started=bool(meeting["current_transcript_version_id"]),
            )
        )
    with op.batch_alter_table("summaries") as batch:
        batch.add_column(
            sa.Column("scope", sa.String(16), nullable=False, server_default="meeting")
        )
        batch.drop_constraint("uq_summary_transcript_mode_template_language", type_="unique")
        batch.create_unique_constraint(
            "uq_summary_input_scope_mode_language",
            [
                "meeting_id",
                "transcript_version_id",
                "scope",
                "mode",
                "template_type",
                "output_language",
            ],
        )


def downgrade() -> None:
    # A populated database with both scopes needs explicit archival before downgrade.
    duplicates = (
        op.get_bind()
        .execute(
            sa.text(
                "SELECT 1 FROM summaries GROUP BY meeting_id, transcript_version_id, mode, "
                "template_type, output_language HAVING COUNT(*) > 1"
            )
        )
        .first()
    )
    if duplicates:
        raise RuntimeError(
            "Archive conflicting summary scopes before downgrading; no summaries were deleted."
        )
    with op.batch_alter_table("summaries") as batch:
        batch.drop_constraint("uq_summary_input_scope_mode_language", type_="unique")
        batch.create_unique_constraint(
            "uq_summary_transcript_mode_template_language",
            [
                "meeting_id",
                "transcript_version_id",
                "mode",
                "template_type",
                "output_language",
            ],
        )
        batch.drop_column("scope")
    op.drop_table("meeting_sections")
