"""Окружение Alembic: откуда брать подключение и описание таблиц."""
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401 — регистрирует все модели в Base.metadata
from app.core.config import get_settings
from app.core.db import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
config.set_main_option("sqlalchemy.url", get_settings().database_url)

target_metadata = Base.metadata

# Таблицы, которые создаёт само расширение PostGIS: Alembic не должен пытаться их удалить
POSTGIS_TABLES = {"spatial_ref_sys"}


def include_object(obj, name, type_, reflected, compare_to):
    return not (type_ == "table" and name in POSTGIS_TABLES)


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, include_object=include_object)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          include_object=include_object, compare_type=True)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
