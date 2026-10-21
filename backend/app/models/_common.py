"""Общие элементы моделей: перечисления, примеси (mixins), типы геометрии.

Перечисления хранятся в базе как текст с ограничением CHECK, а не как типы ENUM
PostgreSQL: добавить новое значение — это правка ограничения, без пересоздания типа.
"""
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.orm import Mapped, mapped_column

SRID = 4326  # WGS-84: так приходят данные из KML и GPS телефона


def enum_col(enum_cls: type[StrEnum], name: str, **kw) -> sa.Enum:
    """Колонка-перечисление: VARCHAR + CHECK со списком допустимых значений."""
    return sa.Enum(
        enum_cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=24,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
        **kw,
    )


def point() -> Geometry:
    return Geometry("POINT", srid=SRID, spatial_index=True)


def linestring() -> Geometry:
    return Geometry("LINESTRING", srid=SRID, spatial_index=True)


class SourceKind(StrEnum):
    """Откуда появился объект в базе."""

    manual = "manual"        # введён вручную
    kml = "kml"              # импорт KML
    asbuilt = "asbuilt"      # пакет исполнительной документации
    docx = "docx"            # импорт рассветки из Word
    generator = "generator"  # генератор тестовых данных


class LengthSourceCode(StrEnum):
    """Источник длины. Чем выше достоверность, тем меньше погрешность."""

    reflectometer = "reflectometer"
    as_built = "as_built"
    passport = "passport"
    engineer = "engineer"
    geometry = "geometry"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        sa.DateTime(timezone=True), server_default=sa.func.now(), onupdate=sa.func.now(), nullable=False
    )


class SourceMixin:
    """Происхождение объекта: вид источника и ссылка на импорт/пакет ИД."""

    source_kind: Mapped[SourceKind] = mapped_column(
        enum_col(SourceKind, "source_kind"), default=SourceKind.manual, nullable=False
    )
    # id в import_job (KML/DOCX) или asbuilt_package (ИД) — без внешнего ключа,
    # т.к. ссылается на разные таблицы в зависимости от source_kind
    source_ref: Mapped[int | None] = mapped_column(sa.Integer)
