"""Общие правила сравнения моделей с базой (для Alembic и теста на расхождение схемы)."""
from geoalchemy2 import alembic_helpers

# Таблицы, которые создаёт само расширение PostGIS: Alembic не должен пытаться их удалить
POSTGIS_TABLES = {"spatial_ref_sys"}


def include_object(obj, name, type_, reflected, compare_to):
    if type_ == "table" and name in POSTGIS_TABLES:
        return False
    # Помощник GeoAlchemy2 скрывает служебные пространственные индексы,
    # которые создаются автоматически вместе с колонкой геометрии
    return alembic_helpers.include_object(obj, name, type_, reflected, compare_to)
