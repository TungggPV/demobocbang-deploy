from pathlib import Path

from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command

BACKEND_ROOT = Path(__file__).resolve().parents[1]


def migration_config(database_url: str) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.set_main_option("sqlalchemy.url", database_url)
    return config


def test_initial_migration_upgrades_and_downgrades_clean_database(tmp_path: Path) -> None:
    database_path = tmp_path / "migration.sqlite3"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(async_url)

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        tables = set(inspect(engine).get_table_names())
        assert {"users", "meetings", "transcripts", "transcript_versions", "summary_versions"} <= tables
        inspector = inspect(engine)
        assert "output_language" in {
            item["name"] for item in inspector.get_columns("summaries")
        }
        assert "uq_summary_transcript_template_language" in {
            item["name"] for item in inspector.get_unique_constraints("summaries")
        }
        assert "ix_meeting_members_user_id_meeting_id" in {
            item["name"] for item in inspector.get_indexes("meeting_members")
        }
        assert "ix_meetings_owner_id_updated_at_created_at" in {
            item["name"] for item in inspector.get_indexes("meetings")
        }
        assert "ix_comments_meeting_id_created_at_active" in {
            item["name"] for item in inspector.get_indexes("comments")
        }
        assert "ix_email_otps_user_id_purpose_created_at_active" in {
            item["name"] for item in inspector.get_indexes("email_otps")
        }
        assert "ix_segment_versions_logical_id_transcript_version_id" in {
            item["name"] for item in inspector.get_indexes("segment_versions")
        }
    finally:
        engine.dispose()

    command.downgrade(config, "base")
    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        assert "users" not in inspect(engine).get_table_names()
    finally:
        engine.dispose()


def test_summary_language_migration_backfills_from_meeting(tmp_path: Path) -> None:
    database_path = tmp_path / "summary-language.sqlite3"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(async_url)
    command.upgrade(config, "20261005_0009")

    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    identifiers = {
        "user_id": "10000000000000000000000000000001",
        "meeting_id": "20000000000000000000000000000002",
        "transcript_id": "30000000000000000000000000000003",
        "version_id": "40000000000000000000000000000004",
        "summary_id": "50000000000000000000000000000005",
    }
    try:
        with engine.begin() as connection:
            connection.execute(
                text("INSERT INTO users (id, email, full_name) VALUES (:user_id, 'old@example.com', 'Old User')"),
                identifiers,
            )
            connection.execute(
                text("INSERT INTO meetings (id, owner_id, title, source_type, language_mode) VALUES (:meeting_id, :user_id, 'Old meeting', 'upload', 'en')"),
                identifiers,
            )
            connection.execute(
                text("INSERT INTO transcripts (id, meeting_id) VALUES (:transcript_id, :meeting_id)"),
                identifiers,
            )
            connection.execute(
                text("INSERT INTO transcript_versions (id, transcript_id, version_no, source) VALUES (:version_id, :transcript_id, 1, 'stt')"),
                identifiers,
            )
            connection.execute(
                text("INSERT INTO summaries (id, meeting_id, transcript_version_id, template_type) VALUES (:summary_id, :meeting_id, :version_id, 'general')"),
                identifiers,
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            assert connection.execute(text("SELECT output_language FROM summaries")).scalar_one() == "en"
    finally:
        engine.dispose()


def test_upload_status_migrations_backfill_completed_uploads_as_recording(tmp_path: Path) -> None:
    database_path = tmp_path / "editable-upload-status.sqlite3"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(async_url)
    command.upgrade(config, "b326ebb08ce4")

    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    identifiers = {
        "user_id": "10000000000000000000000000000001",
        "upload_id": "20000000000000000000000000000002",
        "realtime_id": "30000000000000000000000000000003",
    }
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, email, full_name) "
                    "VALUES (:user_id, 'status@example.com', 'Status User')"
                ),
                identifiers,
            )
            connection.execute(
                text(
                    "INSERT INTO meetings "
                    "(id, owner_id, title, source_type, language_mode, status) "
                    "VALUES (:upload_id, :user_id, 'Upload', 'upload', 'vi', 'completed')"
                ),
                identifiers,
            )
            connection.execute(
                text(
                    "INSERT INTO meetings "
                    "(id, owner_id, title, source_type, language_mode, status) "
                    "VALUES (:realtime_id, :user_id, 'Realtime', 'realtime', 'vi', 'completed')"
                ),
                identifiers,
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            rows = dict(
                connection.execute(
                    text("SELECT source_type, status FROM meetings ORDER BY source_type")
                ).all()
            )
            assert rows == {"realtime": "completed", "upload": "recording"}
    finally:
        engine.dispose()


def test_recording_default_migration_backfills_created_meetings(tmp_path: Path) -> None:
    database_path = tmp_path / "recording-default.sqlite3"
    async_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    config = migration_config(async_url)
    command.upgrade(config, "20261012_0011")

    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    identifiers = {
        "user_id": "10000000000000000000000000000001",
        "meeting_id": "20000000000000000000000000000002",
    }
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO users (id, email, full_name) "
                    "VALUES (:user_id, 'recording@example.com', 'Recording User')"
                ),
                identifiers,
            )
            connection.execute(
                text(
                    "INSERT INTO meetings "
                    "(id, owner_id, title, source_type, language_mode, status) "
                    "VALUES (:meeting_id, :user_id, 'Draft', 'upload', 'vi', 'created')"
                ),
                identifiers,
            )
    finally:
        engine.dispose()

    command.upgrade(config, "head")
    engine = create_engine(f"sqlite+pysqlite:///{database_path.as_posix()}")
    try:
        with engine.connect() as connection:
            status = connection.execute(
                text("SELECT status FROM meetings WHERE id = :meeting_id"), identifiers
            ).scalar_one()
            assert status == "recording"
    finally:
        engine.dispose()
