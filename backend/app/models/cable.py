"""Кабели, волокна, муфты, кроссы, сварки.

Концы волокна. Волокно проходит от начала кабеля (конец ``a``, у start_node) до его
конца (конец ``b``, у end_node). Каждый конец волокна может быть либо сварен с одним
другим концом, либо заведён на один порт кросса — не более одного подключения.
Это правило проверяет триггер в базе (см. миграцию), а не только код приложения.
"""
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models._common import LengthSourceCode, SourceMixin, TimestampMixin, enum_col, linestring
from app.models.network import NetworkNode, RouteSegment
from app.models.reference import CableType, Operator


class FiberEnd(StrEnum):
    a = "a"  # у начала кабеля (start_node)
    b = "b"  # у конца кабеля (end_node)


class Cable(SourceMixin, TimestampMixin, Base):
    __tablename__ = "cable"
    __table_args__ = (
        sa.CheckConstraint("twist_factor >= 0 AND twist_factor < 0.2", name="twist_range"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(200), index=True)  # «СЛ 16000149/03»
    operator_id: Mapped[int] = mapped_column(sa.ForeignKey("operator.id"), index=True)
    cable_type_id: Mapped[int] = mapped_column(sa.ForeignKey("cable_type.id"))
    # Коэффициент укрутки: оптическая длина = физическая × (1 + twist_factor)
    twist_factor: Mapped[float] = mapped_column(sa.Float, default=0.005)
    # Погрешность укрутки (2σ) и откуда значение: None — по умолчанию, «calibration», «manual»
    twist_err_2s: Mapped[float | None] = mapped_column(sa.Float)
    twist_source: Mapped[str | None] = mapped_column(sa.String(20))
    start_node_id: Mapped[int | None] = mapped_column(sa.ForeignKey("network_node.id"))
    end_node_id: Mapped[int | None] = mapped_column(sa.ForeignKey("network_node.id"))
    # Геометрия кабеля — производная от маршрута (кэш для быстрого отображения)
    geom = mapped_column(linestring(), nullable=True)
    lay_year: Mapped[int | None] = mapped_column(sa.SmallInteger)
    note: Mapped[str | None] = mapped_column(sa.Text)

    operator: Mapped[Operator] = relationship()
    cable_type: Mapped[CableType] = relationship()
    start_node: Mapped[NetworkNode | None] = relationship(foreign_keys=[start_node_id])
    end_node: Mapped[NetworkNode | None] = relationship(foreign_keys=[end_node_id])
    route: Mapped[list["CableRoute"]] = relationship(
        back_populates="cable", order_by="CableRoute.seq", cascade="all, delete-orphan"
    )
    fibers: Mapped[list["Fiber"]] = relationship(
        back_populates="cable", order_by="Fiber.number", cascade="all, delete-orphan"
    )
    reserves: Mapped[list["CableReserve"]] = relationship(back_populates="cable", cascade="all, delete-orphan")


class CableRoute(Base):
    """Упорядоченная цепочка участков, по которым идёт кабель."""

    __tablename__ = "cable_route"
    __table_args__ = (
        sa.UniqueConstraint("cable_id", "seq"),
        sa.CheckConstraint("direction IN (1, -1)", name="direction_sign"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cable_id: Mapped[int] = mapped_column(sa.ForeignKey("cable.id", ondelete="CASCADE"))
    seq: Mapped[int] = mapped_column(sa.SmallInteger)
    segment_id: Mapped[int] = mapped_column(sa.ForeignKey("route_segment.id"), index=True)
    # +1 — кабель идёт по участку от node_a к node_b, -1 — в обратную сторону
    direction: Mapped[int] = mapped_column(sa.SmallInteger, default=1)
    channel_no: Mapped[int | None] = mapped_column(sa.SmallInteger)  # занятый канал канализации

    cable: Mapped[Cable] = relationship(back_populates="route")
    segment: Mapped[RouteSegment] = relationship()


class CableReserve(Base):
    """Запас кабеля в узле (в колодце, на опоре, в муфте)."""

    __tablename__ = "cable_reserve"
    __table_args__ = (
        sa.CheckConstraint("length_m > 0", name="length_positive"),
        sa.UniqueConstraint("cable_id", "node_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cable_id: Mapped[int] = mapped_column(sa.ForeignKey("cable.id", ondelete="CASCADE"), index=True)
    node_id: Mapped[int] = mapped_column(sa.ForeignKey("network_node.id"), index=True)
    length_m: Mapped[float] = mapped_column(sa.Float)
    length_source: Mapped[LengthSourceCode] = mapped_column(sa.ForeignKey("length_source.code"))

    cable: Mapped[Cable] = relationship(back_populates="reserves")
    node: Mapped[NetworkNode] = relationship()


class Fiber(Base):
    __tablename__ = "fiber"
    __table_args__ = (sa.UniqueConstraint("cable_id", "number"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    cable_id: Mapped[int] = mapped_column(sa.ForeignKey("cable.id", ondelete="CASCADE"), index=True)
    number: Mapped[int] = mapped_column(sa.SmallInteger)       # сквозной номер 1..N
    module_no: Mapped[int] = mapped_column(sa.SmallInteger)    # номер модуля
    number_in_module: Mapped[int] = mapped_column(sa.SmallInteger)
    color: Mapped[str | None] = mapped_column(sa.String(30))
    module_color: Mapped[str | None] = mapped_column(sa.String(30))

    cable: Mapped[Cable] = relationship(back_populates="fibers")


class Closure(SourceMixin, TimestampMixin, Base):
    """Оптическая муфта, установленная в узле."""

    __tablename__ = "closure"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(sa.ForeignKey("network_node.id"), index=True)
    number: Mapped[str] = mapped_column(sa.String(60))  # «М1», «М 17/10»
    model: Mapped[str | None] = mapped_column(sa.String(120))
    operator_id: Mapped[int | None] = mapped_column(sa.ForeignKey("operator.id"))
    note: Mapped[str | None] = mapped_column(sa.Text)

    node: Mapped[NetworkNode] = relationship()
    operator: Mapped[Operator | None] = relationship()
    splices: Mapped[list["Splice"]] = relationship(back_populates="closure", cascade="all, delete-orphan")


class OpticalCross(SourceMixin, TimestampMixin, Base):
    """Оптический кросс (ODF) в узле."""

    __tablename__ = "optical_cross"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int] = mapped_column(sa.ForeignKey("network_node.id"), index=True)
    name: Mapped[str] = mapped_column(sa.String(60))   # «ODF-1», «ODF-4»
    model: Mapped[str | None] = mapped_column(sa.String(120))
    port_count: Mapped[int] = mapped_column(sa.SmallInteger)
    connector: Mapped[str | None] = mapped_column(sa.String(20))  # «SC/UPC»
    operator_id: Mapped[int | None] = mapped_column(sa.ForeignKey("operator.id"))

    node: Mapped[NetworkNode] = relationship()
    operator: Mapped[Operator | None] = relationship()
    ports: Mapped[list["CrossPort"]] = relationship(
        back_populates="cross", order_by="CrossPort.port_no", cascade="all, delete-orphan"
    )


class CrossPort(Base):
    __tablename__ = "cross_port"
    __table_args__ = (
        sa.UniqueConstraint("cross_id", "port_no"),
        sa.CheckConstraint("(fiber_id IS NULL) = (fiber_end IS NULL)", name="fiber_end_pair"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    cross_id: Mapped[int] = mapped_column(sa.ForeignKey("optical_cross.id", ondelete="CASCADE"))
    port_no: Mapped[int] = mapped_column(sa.SmallInteger)
    fiber_id: Mapped[int | None] = mapped_column(sa.ForeignKey("fiber.id", ondelete="SET NULL"), index=True)
    fiber_end: Mapped[FiberEnd | None] = mapped_column(enum_col(FiberEnd, "fiber_end_port"))
    note: Mapped[str | None] = mapped_column(sa.String(200))

    cross: Mapped[OpticalCross] = relationship(back_populates="ports")
    fiber: Mapped[Fiber | None] = relationship()


class Splice(Base):
    """Сварка двух концов волокон в муфте (или в кроссе — пигтейл/транзит)."""

    __tablename__ = "splice"
    __table_args__ = (
        sa.CheckConstraint("(closure_id IS NULL) <> (cross_id IS NULL)", name="one_place"),
        sa.CheckConstraint("fiber_a_id <> fiber_b_id", name="distinct_fibers"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    closure_id: Mapped[int | None] = mapped_column(sa.ForeignKey("closure.id", ondelete="CASCADE"), index=True)
    cross_id: Mapped[int | None] = mapped_column(sa.ForeignKey("optical_cross.id", ondelete="CASCADE"))
    fiber_a_id: Mapped[int] = mapped_column(sa.ForeignKey("fiber.id", ondelete="CASCADE"), index=True)
    end_a: Mapped[FiberEnd] = mapped_column(enum_col(FiberEnd, "fiber_end_a"))
    fiber_b_id: Mapped[int] = mapped_column(sa.ForeignKey("fiber.id", ondelete="CASCADE"), index=True)
    end_b: Mapped[FiberEnd] = mapped_column(enum_col(FiberEnd, "fiber_end_b"))
    loss_splicer_db: Mapped[float | None] = mapped_column(sa.Float)
    loss_otdr_ab_db: Mapped[float | None] = mapped_column(sa.Float)
    loss_otdr_ba_db: Mapped[float | None] = mapped_column(sa.Float)
    note: Mapped[str | None] = mapped_column(sa.String(300))  # «куда идёт сигнал»

    closure: Mapped[Closure | None] = relationship(back_populates="splices")
    fiber_a: Mapped[Fiber] = relationship(foreign_keys=[fiber_a_id])
    fiber_b: Mapped[Fiber] = relationship(foreign_keys=[fiber_b_id])


class Jumper(TimestampMixin, Base):
    """Перемычка (линия с подписью) между двумя любыми объектами карты."""

    __tablename__ = "jumper"

    id: Mapped[int] = mapped_column(primary_key=True)
    a_type: Mapped[str] = mapped_column(sa.String(30))  # node | closure | cross | access_node (как типы карточек)
    a_id: Mapped[int] = mapped_column(sa.Integer)
    b_type: Mapped[str] = mapped_column(sa.String(30))
    b_id: Mapped[int] = mapped_column(sa.Integer)
    geom = mapped_column(linestring(), nullable=False)
    label: Mapped[str | None] = mapped_column(sa.String(200))  # «Резерв 24 волокна»
