"""Upgrade and downgrade the hco-core schema with Alembic."""

from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import Connection, create_engine

from hco_core.settings import CoreSettings

ALEMBIC_INI = Path(__file__).with_name("alembic.ini")


def _alembic_config(connection: Connection) -> Config:
    """Return the Alembic configuration that runs on the given connection."""
    config = Config(ALEMBIC_INI)
    config.attributes["connection"] = connection
    return config


def upgrade(settings: CoreSettings, revision: str = "head") -> None:
    """Upgrade the schema to the revision, in one transaction."""
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as connection:
            command.upgrade(_alembic_config(connection), revision)
    finally:
        engine.dispose()


def downgrade(settings: CoreSettings, revision: str) -> None:
    """Downgrade the schema to the revision, in one transaction."""
    engine = create_engine(settings.database_url.get_secret_value())
    try:
        with engine.begin() as connection:
            command.downgrade(_alembic_config(connection), revision)
    finally:
        engine.dispose()
