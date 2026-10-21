"""Справочники: владельцы инфраструктуры, операторы, источники длин, маркировка, типы кабеля."""
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models._common import LengthSourceCode, enum_col


class Owner(Base):
    """Владелец инфраструктуры: кабельной канализации, колодцев, опор (Таттелеком, Телесет…)."""

    __tablename__ = "owner"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(120), unique=True)
    short_name: Mapped[str] = mapped_column(sa.String(40))
    color: Mapped[str] = mapped_column(sa.String(7), default="#7f8c8d")  # цвет на карте, #RRGGBB


class Operator(Base):
    """Оператор связи — владелец кабелей (Ростелеком, МТС…)."""

    __tablename__ = "operator"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(120), unique=True)
    short_name: Mapped[str] = mapped_column(sa.String(40))
    color: Mapped[str] = mapped_column(sa.String(7), default="#2c3e50")


class LengthSource(Base):
    """Источник длины и его погрешность по умолчанию (основа интервала погрешности локализации)."""

    __tablename__ = "length_source"

    code: Mapped[LengthSourceCode] = mapped_column(enum_col(LengthSourceCode, "length_source_code"),
                                                   primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(80))
    default_error_pct: Mapped[float] = mapped_column(sa.Float)  # 1.0 = ±1 %
    priority: Mapped[int] = mapped_column(sa.SmallInteger)       # меньше — достовернее


class ColorKind(StrEnum):
    module = "module"
    fiber = "fiber"


class ColorScheme(Base):
    """Схема цветовой маркировки модулей и волокон (зависит от производителя кабеля)."""

    __tablename__ = "color_scheme"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(120), unique=True)
    entries: Mapped[list["ColorSchemeEntry"]] = relationship(
        back_populates="scheme", order_by="ColorSchemeEntry.position", cascade="all, delete-orphan"
    )

    def color_of(self, kind: ColorKind, position: int) -> "ColorSchemeEntry | None":
        """Цвет модуля/волокна по номеру (1..N); после N цвета повторяются."""
        items = [e for e in self.entries if e.kind == kind]
        return items[(position - 1) % len(items)] if items else None


class ColorSchemeEntry(Base):
    __tablename__ = "color_scheme_entry"
    __table_args__ = (sa.UniqueConstraint("scheme_id", "kind", "position"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    scheme_id: Mapped[int] = mapped_column(sa.ForeignKey("color_scheme.id", ondelete="CASCADE"))
    kind: Mapped[ColorKind] = mapped_column(enum_col(ColorKind, "color_kind"))
    position: Mapped[int] = mapped_column(sa.SmallInteger)
    color_name: Mapped[str] = mapped_column(sa.String(30))
    hex: Mapped[str] = mapped_column(sa.String(7))

    scheme: Mapped[ColorScheme] = relationship(back_populates="entries")


class CableType(Base):
    """Марка кабеля: модульная структура N модулей × M волокон."""

    __tablename__ = "cable_type"
    __table_args__ = (
        sa.CheckConstraint("modules > 0 AND fibers_per_module > 0", name="structure_positive"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    mark: Mapped[str] = mapped_column(sa.String(120), unique=True)  # «ОКГ-8 (G.652.D)-T/П 3,0»
    modules: Mapped[int] = mapped_column(sa.SmallInteger)
    fibers_per_module: Mapped[int] = mapped_column(sa.SmallInteger)
    color_scheme_id: Mapped[int | None] = mapped_column(sa.ForeignKey("color_scheme.id"))
    note: Mapped[str | None] = mapped_column(sa.Text)

    color_scheme: Mapped[ColorScheme | None] = relationship()

    @property
    def fiber_count(self) -> int:
        return self.modules * self.fibers_per_module
