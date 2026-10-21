"""Общие фикстуры тестов.

Тесты работают с настоящим PostgreSQL + PostGIS (как в продакшене), а не с SQLite:
пространственные запросы (ST_DWithin, geography) в SQLite не проверить.
Адрес тестовой базы задаётся переменной DATABASE_URL; база создаётся миграциями
один раз за прогон.
"""
import os

import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(scope="session", autouse=True)
def migrated_db():
    """Применяет все миграции к тестовой базе перед тестами."""
    cfg = Config(os.path.join(BACKEND_DIR, "alembic.ini"))
    cfg.set_main_option("script_location", os.path.join(BACKEND_DIR, "alembic"))
    command.upgrade(cfg, "head")
    yield


@pytest.fixture()
def client():
    from app.main import app

    with TestClient(app) as c:
        yield c
