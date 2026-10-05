"""Asynchronous SQLAlchemy engine and session management."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine

SessionFactory = async_sessionmaker[AsyncSession]


class Database:
    """Owns the async engine and produces short-lived sessions.

    Sessions are never shared between concurrent tasks: every unit of work opens
    its own session from :attr:`session_factory` and closes it promptly.
    """

    def __init__(self, url: str, *, echo: bool = False, pool_size: int = 10) -> None:
        self._engine: AsyncEngine = create_async_engine(
            url,
            echo=echo,
            pool_pre_ping=True,
            pool_size=pool_size,
            max_overflow=pool_size,
            future=True,
        )
        self._session_factory: SessionFactory = async_sessionmaker(
            bind=self._engine,
            expire_on_commit=False,
            autoflush=False,
        )

    @property
    def engine(self) -> AsyncEngine:
        """Return the underlying async engine."""
        return self._engine

    @property
    def session_factory(self) -> SessionFactory:
        """Return the session factory used to open short-lived sessions."""
        return self._session_factory

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        """Open a session and guarantee its closure."""
        async with self._session_factory() as session:
            yield session

    async def dispose(self) -> None:
        """Dispose the connection pool."""
        await self._engine.dispose()
