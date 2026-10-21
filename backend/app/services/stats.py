"""Сводка по содержимому базы — для главной страницы и команды `stats`."""
import sqlalchemy as sa
from sqlalchemy.orm import Session

# Подпись → SQL-выражение подсчёта
STAT_QUERIES = {
    "Узлы сети": "SELECT count(*) FROM network_node",
    "Колодцы": "SELECT count(*) FROM network_node WHERE kind = 'well'",
    "Опоры": "SELECT count(*) FROM network_node WHERE kind = 'pole'",
    "Здания": "SELECT count(*) FROM network_node WHERE kind = 'building'",
    "Участки трасс": "SELECT count(*) FROM route_segment",
    "Длина трасс, км": "SELECT round((coalesce(sum(geom_length_m), 0) / 1000)::numeric, 1) FROM route_segment",
    "Участки с известной длиной": "SELECT count(*) FROM route_segment WHERE fact_length_m IS NOT NULL",
    "Кабели": "SELECT count(*) FROM cable",
    "Волокна": "SELECT count(*) FROM fiber",
    "Муфты": "SELECT count(*) FROM closure",
    "Сварки": "SELECT count(*) FROM splice",
    "Кроссы": "SELECT count(*) FROM optical_cross",
    "Узлы доступа": "SELECT count(*) FROM access_node",
    "Измерения трактов": "SELECT count(*) FROM measurement",
    "Пользователи": "SELECT count(*) FROM app_user",
}


def collect_stats(db: Session) -> dict[str, float]:
    return {label: db.execute(sa.text(sql)).scalar_one() for label, sql in STAT_QUERIES.items()}
