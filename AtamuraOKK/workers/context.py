"""Shared worker plumbing: async session factory + helpers."""

from __future__ import annotations

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from AtamuraOKK.settings import settings


def build_engine() -> AsyncEngine:
    """Create the async engine for worker processes (separate from the API)."""
    return create_async_engine(str(settings.db_url), echo=settings.db_echo)


def session_factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Build a session maker bound to ``engine``."""
    return async_sessionmaker(engine, expire_on_commit=False)


def safe_stem(call_id: str) -> str:
    """Filesystem-safe stem from a Bitrix CALL_ID."""
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in call_id)
