"""Подключение к базе данных.

engine  — пул соединений с PostgreSQL;
SessionLocal — фабрика сессий (одна сессия на HTTP-запрос);
Base    — базовый класс ORM-моделей, от него наследуются все таблицы.
"""
from collections.abc import Iterator

from sqlalchemy import MetaData, create_engine
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.core.config import get_settings

# Единые правила имён ограничений: Alembic генерирует предсказуемые миграции,
# а ошибки БД вида «ck_splice_fiber_distinct» сразу понятно к чему относятся.
NAMING = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING)


engine = create_engine(get_settings().database_url, pool_pre_ping=True, future=True)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db() -> Iterator[Session]:
    """Зависимость FastAPI: открывает сессию на время запроса и закрывает после."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
