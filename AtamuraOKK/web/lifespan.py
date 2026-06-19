from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from AtamuraOKK.db.models import load_all_models
from AtamuraOKK.settings import settings


def _setup_db(app: FastAPI) -> None:  # pragma: no cover
    """
    Creates connection to the database.

    This function creates SQLAlchemy engine instance,
    session_factory for creating sessions
    and stores them in the application's state property.

    :param app: fastAPI application.
    """
    engine = create_async_engine(str(settings.db_url), echo=settings.db_echo)
    session_factory = async_sessionmaker(
        engine,
        expire_on_commit=False,
    )
    app.state.db_engine = engine
    app.state.db_session_factory = session_factory


@asynccontextmanager
async def lifespan_setup(
    app: FastAPI,
) -> AsyncGenerator[None, None]:  # pragma: no cover
    """
    Actions to run on application startup.

    This function uses fastAPI app to store data
    in the state, such as db_engine.

    :param app: the fastAPI application.
    :return: function that actually performs actions.
    """

    # Register every ORM model so Base.metadata is complete. The read API runs
    # on raw SQL over views, so it would otherwise only import the handful of
    # models it writes (e.g. Appeal) — leaving cross-table FK targets like
    # ``calls`` unregistered and any appeal write failing to resolve its FK.
    load_all_models()

    app.middleware_stack = None
    _setup_db(app)
    app.middleware_stack = app.build_middleware_stack()

    yield
    await app.state.db_engine.dispose()
