"""Окружение Alembic: откуда брать подключение и описание таблиц."""
from logging.config import fileConfig

from alembic import context
from geoalchemy2 import alembic_helpers
from sqlalchemy import engine_from_config, pool

from app import models  # noqa: F401 — регистрирует все модели в Base.metadata
from app.core.config import get_settings
from app.core.db import Base
from app.core.migrations import include_object

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)
# Адрес базы — из настроек; эксперимент (этап 12) передаёт свою отдельную базу через attributes
config.set_main_option("sqlalchemy.url", config.attributes.get("database_url") or get_settings().database_url)

target_metadata = Base.metadata

# Помощники GeoAlchemy2: корректно создают таблицы с геометрией и их GiST-индексы
GEO_OPTS = {
    "process_revision_directives": alembic_helpers.writer,
    "render_item": alembic_helpers.render_item,
}


def run_migrations_offline() -> None:
    context.configure(url=config.get_main_option("sqlalchemy.url"), target_metadata=target_metadata,
                      literal_binds=True, include_object=include_object, **GEO_OPTS)
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(config.get_section(config.config_ini_section, {}),
                                     prefix="sqlalchemy.", poolclass=pool.NullPool)
    with connectable.connect() as connection:
        context.configure(connection=connection, target_metadata=target_metadata,
                          include_object=include_object, compare_type=True, **GEO_OPTS)
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
