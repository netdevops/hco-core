"""Async engines and sessions on the hco-core database."""

from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from hco_core.settings import CoreSettings


def create_database_engine(settings: CoreSettings) -> AsyncEngine:
    """Return an async psycopg engine on `HCO_DATABASE_URL`."""
    return create_async_engine(
        settings.database_url.get_secret_value(), pool_pre_ping=True
    )


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Return a session factory whose objects stay readable after a commit."""
    return async_sessionmaker(engine, expire_on_commit=False)
