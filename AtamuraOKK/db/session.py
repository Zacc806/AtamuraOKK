"""Standalone async DB session factory for workers/CLIs (no FastAPI app needed)."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from functools import lru_cache

from sqlalchemy.ext.asyncio import (
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from AtamuraOKK.db.models import load_all_models
from AtamuraOKK.settings import settings


@lru_cache(maxsize=1)
def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """Process-wide async session factory."""
    # Import every model so cross-table FKs (e.g. calls.manager_id) resolve even
    # when a worker only imports a subset of models directly.
    load_all_models()
    engine = create_async_engine(
        str(settings.db_url),
        echo=settings.db_echo,
        pool_size=settings.db_pool_size,
        max_overflow=settings.db_max_overflow,
        pool_pre_ping=True,  # survive idle drops / Postgres restarts
        pool_recycle=1800,
    )
    return async_sessionmaker(engine, expire_on_commit=False)


@asynccontextmanager
async def session_scope() -> AsyncGenerator[AsyncSession, None]:
    """Transactional session: commit on success, rollback on error."""
    factory = get_session_factory()
    session = factory()
    try:
        yield session
        await session.commit()
    except Exception:
        await session.rollback()
        raise
    finally:
        await session.close()
