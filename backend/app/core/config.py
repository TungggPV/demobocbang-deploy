"""Environment-backed application settings."""

from functools import lru_cache
from pathlib import Path
from typing import Literal
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

REPOSITORY_ROOT = Path(__file__).resolve().parents[3]
API_V1_PREFIX = "/api/v1"


class Settings(BaseSettings):
    """Runtime configuration with safe local defaults and no embedded secrets."""

    model_config = SettingsConfigDict(
        env_file=REPOSITORY_ROOT / ".env",
        env_file_encoding="utf-8",
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "Bóc Băng AI API"
    environment: str = Field(
        default="development",
        validation_alias=AliasChoices("APP_ENVIRONMENT", "ENVIRONMENT"),
    )
    log_level: str = "INFO"
    performance_query_timing_enabled: bool = False
    performance_slow_query_ms: float = 100.0
    frontend_origins: str | tuple[str, ...] = Field(
        default=("http://localhost:5173",),
        validation_alias=AliasChoices("FRONTEND_ORIGINS", "FRONTEND_ORIGIN"),
    )
    database_url: str | None = None
    redis_url: str | None = None
    queue_backend: str = "redis"
    worker_max_retries: int = 3
    worker_retry_min_backoff_ms: int = 10_000
    worker_retry_max_backoff_ms: int = 60_000
    worker_reconcile_interval_seconds: int = 15
    worker_reconcile_batch_size: int = 100
    worker_delivery_timeout_seconds: int = 300
    jwt_secret: str | None = None
    access_token_ttl_seconds: int = 900
    refresh_token_ttl_seconds: int = 2_592_000
    media_playback_ticket_ttl_seconds: int = 300
    email_otp_ttl_seconds: int = 600
    email_otp_max_attempts: int = 5
    email_provider: str | None = None
    email_api_key: str | None = None
    email_from: str | None = None
    email_smtp_host: str | None = None
    email_smtp_port: int = 587
    email_smtp_username: str | None = None
    email_smtp_password: str | None = None
    email_smtp_use_tls: bool = True
    email_attachment_max_bytes: int = 15_000_000
    google_client_id: str | None = None
    google_client_secret: str | None = None
    google_redirect_uri: str | None = None
    max_upload_bytes: int = 524_288_000
    max_media_duration_seconds: int = 7_200
    storage_backend: str = "local"
    media_storage_path: Path = REPOSITORY_ROOT / ".local-media"
    s3_endpoint_url: str | None = Field(
        default=None, validation_alias=AliasChoices("S3_ENDPOINT_URL", "S3_ENDPOINT")
    )
    s3_access_key_id: str | None = Field(
        default=None, validation_alias=AliasChoices("S3_ACCESS_KEY_ID", "S3_ACCESS_KEY")
    )
    s3_secret_access_key: str | None = Field(
        default=None, validation_alias=AliasChoices("S3_SECRET_ACCESS_KEY", "S3_SECRET_KEY")
    )
    s3_bucket_name: str | None = Field(
        default=None, validation_alias=AliasChoices("S3_BUCKET_NAME", "S3_BUCKET")
    )
    s3_region: str = "auto"
    storage_presigned_url_ttl_seconds: int = 300
    stt_provider: str | None = None
    stt_api_key: str | None = None
    stt_model: str = "universal-3-5-pro"
    stt_fallback_model: str | None = "universal-2"
    assemblyai_api_base_url: str = "https://api.assemblyai.com/v2"
    assemblyai_streaming_base_url: str = "wss://streaming.assemblyai.com/v3/ws"
    assemblyai_streaming_model: str = "universal-3-6-pro"
    assemblyai_streaming_mode: Literal["min_latency", "balanced", "max_accuracy"] = "max_accuracy"
    assemblyai_streaming_min_turn_silence_ms: int = 560
    assemblyai_streaming_max_turn_silence_ms: int = 2_000
    stt_poll_interval_seconds: float = 2.0
    stt_poll_timeout_seconds: int = 1800
    translation_provider: str | None = None
    translation_api_key: str | None = None
    translation_fallback_provider: str | None = None
    llm_provider: str | None = None
    llm_api_key: str | None = None
    llm_model: str | None = None
    llm_fallback_provider: str | None = None
    gemini_model: str = "gemini-2.5-flash"
    gemini_api_base_url: str = "https://generativelanguage.googleapis.com/v1beta"
    groq_api_key: str | None = None
    groq_model: str = "openai/gpt-oss-20b"
    groq_api_base_url: str = "https://api.groq.com/openai/v1"

    @field_validator("frontend_origins", mode="before")
    @classmethod
    def split_frontend_origins(cls, value: str | list[str] | tuple[str, ...]) -> tuple[str, ...]:
        if isinstance(value, str):
            origins = tuple(
                origin.strip().rstrip("/") for origin in value.split(",") if origin.strip()
            )
        else:
            origins = tuple(
                str(origin).strip().rstrip("/") for origin in value if str(origin).strip()
            )
        if not origins:
            raise ValueError("At least one FRONTEND_ORIGIN must be configured.")
        return origins

    @field_validator("database_url", mode="before")
    @classmethod
    def normalize_database_url(cls, value: str | None) -> str | None:
        if not value:
            return None
        if value.startswith("postgres://"):
            value = "postgresql+asyncpg://" + value.removeprefix("postgres://")
        elif value.startswith("postgresql://"):
            value = "postgresql+asyncpg://" + value.removeprefix("postgresql://")

        if not value.startswith("postgresql+asyncpg://"):
            return value

        parsed = urlsplit(value)
        query: list[tuple[str, str]] = []
        for key, query_value in parse_qsl(parsed.query, keep_blank_values=True):
            # Neon emits libpq options that SQLAlchemy forwards as keyword
            # arguments. asyncpg expects `ssl` and does not support
            # `channel_binding`, so translate the URL at the configuration
            # boundary before either Alembic or the application creates an
            # engine.
            if key == "channel_binding":
                continue
            query.append(("ssl" if key == "sslmode" else key, query_value))

        return urlunsplit(parsed._replace(query=urlencode(query)))

    @field_validator("storage_backend")
    @classmethod
    def validate_storage_backend(cls, value: str) -> str:
        backend = value.strip().lower()
        if backend not in {"local", "s3"}:
            raise ValueError("STORAGE_BACKEND must be either 'local' or 's3'.")
        return backend

    @field_validator("queue_backend")
    @classmethod
    def validate_queue_backend(cls, value: str) -> str:
        backend = value.strip().lower()
        if backend != "redis":
            raise ValueError("QUEUE_BACKEND must be 'redis'.")
        return backend

    @field_validator(
        "worker_max_retries",
        "worker_retry_min_backoff_ms",
        "worker_retry_max_backoff_ms",
        "worker_reconcile_interval_seconds",
        "worker_reconcile_batch_size",
        "worker_delivery_timeout_seconds",
        "assemblyai_streaming_min_turn_silence_ms",
        "assemblyai_streaming_max_turn_silence_ms",
    )
    @classmethod
    def validate_positive_worker_setting(cls, value: int) -> int:
        if value <= 0:
            raise ValueError(
                "Worker retry, delivery, and reconciliation settings must be positive."
            )
        return value

    def require_queue_configuration(self) -> None:
        if self.environment.lower() in {"production", "prod"} and not self.redis_url:
            raise ValueError("REDIS_URL is required when APP_ENVIRONMENT is production.")
        if not self.redis_url:
            raise ValueError("REDIS_URL is required to enqueue background jobs.")


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide settings object."""
    return Settings()
