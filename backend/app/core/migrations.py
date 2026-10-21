"""Общие правила сравнения моделей с базой (для Alembic и теста на расхождение схемы)."""
from geoalchemy2 import alembic_helpers

# Таблицы, которые создаёт само расширение PostGIS: Alembic не должен пытаться их удалить
POSTGIS_TABLES = {"spatial_ref_sys"}


def _project_table_names() -> set[str]:
    from app.core.db import Base

    return set(Base.metadata.tables)


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in POSTGIS_TABLES:
        return False
    # Образ postgis/postgis дополнительно ставит расширения (геокодер TIGER, topology) со своими
    # таблицами. Они не часть проекта: сравниваем только таблицы, описанные в моделях.
    if type_ == "table" and reflected and compare_to is None and name not in _project_table_names():
        return False
    if type_ == "index" and reflected and compare_to is None and obj.table.name not in _project_table_names():
        return False
    # Помощник GeoAlchemy2 скрывает служебные пространственные индексы,
    # которые создаются автоматически вместе с колонкой геометрии
    return alembic_helpers.include_object(obj, name, type_, reflected, compare_to)
