"""Alembic environment: uses akunaki Settings + sqlite+libsql engine."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from akunaki.adapters.db import models as _models  # noqa: F401 — register models
from akunaki.adapters.db.base import Base
from akunaki.adapters.db.engine import create_db_engine
from akunaki.config import Settings, get_settings

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _settings_from_context() -> Settings:
    """Prefer AKUNAKI_* / process settings over alembic.ini URL."""
    return get_settings()


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    settings = _settings_from_context()
    context.configure(
        url=settings.database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        render_as_batch=True,
    )

    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode with the official libSQL engine."""
    settings = _settings_from_context()
    connectable = create_db_engine(settings)

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            render_as_batch=True,
        )

        with context.begin_transaction():
            context.run_migrations()

    connectable.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
