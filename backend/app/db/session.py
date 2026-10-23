import asyncio
"""Async SQLAlchemy connection and session lifecycle."""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

from app.core.logging import get_logger
from app.core.middleware import request_id_context

logger = get_logger(__name__)


class Database:
    """Lazy database resource that keeps application startup independent of network availability."""

    def __init__(
        self,
        database_url: str | None,
        *,
        query_timing_enabled: bool = False,
        slow_query_ms: float = 100.0,
    ) -> None:
        if database_url:
            if database_url.startswith("sqlite"):
                self._engine: AsyncEngine | None = create_async_engine(
                    database_url,
                )
            else:
                connect_args = {
                    "statement_cache_size": 0,
                    "prepared_statement_cache_size": 0,
                    "command_timeout": 30,
                }
                self._engine: AsyncEngine | None = create_async_engine(
                    database_url,
                    pool_size=25,
                    max_overflow=25,
                    pool_timeout=30,
                    pool_recycle=120,
                    pool_pre_ping=True,
                    connect_args=connect_args,
                )
        else:
            self._engine = None

        if self._engine is not None and query_timing_enabled:
            self._install_query_timing(self._engine, slow_query_ms)
        self._session_factory = (
            async_sessionmaker(self._engine, expire_on_commit=False) if self._engine else None
        )

    async def prewarm(self, count: int = 6) -> None:
        """Pre-warm connections in parallel so user requests don't suffer the 4s TLS handshake."""
        if self._engine is None:
            return
        async def _connect():
            try:
                async with self.session() as s:
                    await s.execute(text("SELECT 1"))
            except Exception:
                pass
        await asyncio.gather(*[_connect() for _ in range(count)])

    @property
    def is_configured(self) -> bool:
        return self._engine is not None

    async def ping(self) -> bool:
        if self._engine is None:
            return False
        try:
            async with self._engine.connect() as connection:
                await connection.execute(text("SELECT 1"))
        except Exception:
            return False
        return True

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        if self._session_factory is None:
            raise RuntimeError("Database is not configured.")
        async with self._session_factory() as session:
            yield session

    async def dispose(self) -> None:
        if self._engine is not None:
            await self._engine.dispose()

    @staticmethod
    def _install_query_timing(engine: AsyncEngine, slow_query_ms: float) -> None:
        """Log only slow SQL shape and duration; never bind values or SQL text."""

        @event.listens_for(engine.sync_engine, "before_cursor_execute")
        def before_cursor_execute(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: object,
        ) -> None:  # noqa: ARG001
            context._performance_query_started_at = perf_counter()  # type: ignore[attr-defined]

        @event.listens_for(engine.sync_engine, "after_cursor_execute")
        def after_cursor_execute(
            conn: object,
            cursor: object,
            statement: str,
            parameters: object,
            context: object,
            executemany: object,
        ) -> None:  # noqa: ARG001
            started_at = getattr(context, "_performance_query_started_at", None)
            if started_at is None:
                return
            duration_ms = round((perf_counter() - started_at) * 1000, 2)
            if duration_ms >= slow_query_ms:
                logger.warning(
                    "slow_database_query",
                    extra={
                        "duration_ms": duration_ms,
                        "operation": (
                            statement.lstrip().split(None, 1)[0].upper()
                            if statement.strip()
                            else "UNKNOWN"
                        ),
                        "batch": bool(executemany),
                        "request_id": request_id_context.get(),
                    },
                )
