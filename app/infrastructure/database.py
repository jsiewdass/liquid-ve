"""Async database engine, session factories and health helpers."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from typing import Any

from loguru import logger
from sqlalchemy import event, text
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import Settings, get_settings
from app.core.exceptions import ConfigurationError
from app.infrastructure.models import Base

engine: AsyncEngine | None = None
AsyncSessionLocal: async_sessionmaker[AsyncSession] | None = None


def _is_sqlite(url: str) -> bool:
    """Return whether a database URL targets SQLite.

    Args:
        url: SQLAlchemy database URL.

    Returns:
        ``True`` for ``sqlite`` and ``sqlite+aiosqlite`` URLs.
    """
    return url.split(":", 1)[0].startswith("sqlite")


def create_engine_from_settings(settings: Settings) -> AsyncEngine:
    """Build an :class:`AsyncEngine` from application settings.

    SQLite receives conservative PRAGMAs (WAL, foreign keys, busy timeout)
    for single-writer safety.  PostgreSQL uses a bounded, pre-pinged pool.

    Args:
        settings: Active application settings.

    Returns:
        A configured asynchronous engine.
    """
    url = settings.database_url
    if _is_sqlite(url):
        async_engine = create_async_engine(
            url,
            echo=settings.database_echo,
            future=True,
            connect_args={"check_same_thread": False, "timeout": 30},
        )

        @event.listens_for(async_engine.sync_engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection: Any, _connection_record: Any) -> None:
            """Apply SQLite PRAGMAs on every new connection."""
            cursor = dbapi_connection.cursor()
            try:
                cursor.execute("PRAGMA foreign_keys=ON")
                cursor.execute("PRAGMA journal_mode=WAL")
                cursor.execute("PRAGMA synchronous=NORMAL")
                cursor.execute("PRAGMA busy_timeout=5000")
            finally:
                cursor.close()

        return async_engine
    return create_async_engine(
        url,
        echo=settings.database_echo,
        future=True,
        pool_pre_ping=True,
        pool_size=settings.database_pool_size,
        max_overflow=settings.database_max_overflow,
        pool_timeout=settings.database_pool_timeout_seconds,
    )


def init_engine(settings: Settings | None = None) -> AsyncEngine:
    """Initialize the module-level engine and session factory once.

    Args:
        settings: Optional settings override; defaults to the cached
            application settings.

    Returns:
        The initialized engine.
    """
    global engine, AsyncSessionLocal
    if engine is not None:
        return engine
    active_settings = settings or get_settings()
    engine = create_engine_from_settings(active_settings)
    AsyncSessionLocal = async_sessionmaker(
        engine,
        expire_on_commit=False,
        autoflush=False,
    )
    return engine


def get_engine() -> AsyncEngine:
    """Return the active engine, initializing it lazily when needed.

    Raises:
        ConfigurationError: If initialization fails unexpectedly.
    """
    if engine is None:
        return init_engine()
    return engine


def _get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Return the active session factory.

    Raises:
        ConfigurationError: If the engine was never initialized.
    """
    if AsyncSessionLocal is None:
        init_engine()
    if AsyncSessionLocal is None:  # pragma: no cover - defensive
        raise ConfigurationError("Database session factory is not initialized")
    return AsyncSessionLocal


async def dispose_engine() -> None:
    """Dispose the module-level engine and reset global state."""
    global engine, AsyncSessionLocal
    if engine is not None:
        await engine.dispose()
    engine = None
    AsyncSessionLocal = None


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI dependency yielding a transactional session.

    Commits on success and rolls back on failure, re-raising the original
    exception so the global handler can render it.

    Yields:
        An :class:`AsyncSession` bound to the request.
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            logger.exception("Database session rolled back")
            raise


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Provide a transactional session for workers and background tasks.

    Yields:
        An :class:`AsyncSession` that commits on clean exit and rolls back on
        error.
    """
    factory = _get_session_factory()
    async with factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


async def check_database_health() -> bool:
    """Execute ``SELECT 1`` to verify database connectivity.

    Returns:
        ``True`` when the database answered, ``False`` otherwise.
    """
    try:
        async with get_engine().connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            return result.scalar_one() == 1
    except Exception as exc:  # noqa: BLE001 - health checks never raise
        logger.debug("Database health check failed: {}", type(exc).__name__)
        return False


async def create_all() -> None:
    """Create every table from metadata.

    Development and test convenience only.  In production the schema is owned
    by Alembic migrations and this function must not be invoked.
    """
    active_engine = get_engine()
    async with active_engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)


__all__ = [
    "engine",
    "AsyncSessionLocal",
    "create_engine_from_settings",
    "init_engine",
    "get_engine",
    "dispose_engine",
    "get_session",
    "session_scope",
    "check_database_health",
    "create_all",
]
