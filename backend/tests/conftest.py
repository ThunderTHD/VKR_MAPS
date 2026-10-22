"""Общие фикстуры тестов.

Тесты работают с настоящим PostgreSQL + PostGIS (как в продакшене), а не с SQLite:
пространственные запросы (ST_DWithin, geography) в SQLite не проверить.
Адрес тестовой базы задаётся переменной DATABASE_URL; база создаётся миграциями
один раз за прогон.
"""
import os
import tempfile

# Загрузки тестов — во временный каталог (до импорта приложения: настройки кэшируются)
os.environ.setdefault("UPLOAD_DIR", tempfile.mkdtemp(prefix="vols_test_uploads_"))
# Фоновый опрос узлов в тестах не нужен: опрос вызывается тестами явно
os.environ.setdefault("MONITORING_AUTOSTART", "false")

import pytest  # noqa: E402
from alembic import command  # noqa: E402
from alembic.config import Config  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from app.core.db import SessionLocal, engine  # noqa: E402

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def alembic_config() -> Config:
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    return cfg


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    """Применяет все миграции к тестовой базе перед тестами."""
    command.upgrade(alembic_config(), "head")
    yield


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c


@pytest.fixture()
def db():
    """Сессия внутри транзакции, которая откатывается после теста: база остаётся чистой."""
    connection = engine.connect()
    transaction = connection.begin()
    session = SessionLocal(bind=connection, join_transaction_mode="create_savepoint")
    try:
        yield session
    finally:
        session.close()
        transaction.rollback()
        connection.close()
