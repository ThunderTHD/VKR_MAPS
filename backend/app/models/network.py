"""Инфраструктура: точки сети (колодцы, опоры, площадки) и участки трасс между ними."""
from datetime import datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models._common import (
    LengthSourceCode,
    SourceMixin,
    TimestampMixin,
    enum_col,
    linestring,
    point,
)
from app.models.reference import Owner


class NodeKind(StrEnum):
    well = "well"          # колодец кабельной канализации
    kks = "kks"            # ККС (кабельная колодезная станция)
    pole = "pole"          # опора
    site = "site"          # узел/площадка (АТС, БС, контейнер)
    building = "building"  # здание (ввод в здание, клиент)
    closure_point = "closure_point"  # муфта вне колодца (на фасаде, на опоре)
    unknown = "unknown"    # точка без типа (непривязанный конец при импорте)


class SegmentKind(StrEnum):
    duct = "duct"            # кабельная канализация
    microduct = "microduct"  # микроканализация
    aerial = "aerial"        # подвес по опорам
    facade = "facade"        # по фасаду
    indoor = "indoor"        # внутри здания
    hdd = "hdd"              # прокол ГНБ
    ground = "ground"        # в грунте


class NetworkNode(SourceMixin, TimestampMixin, Base):
    """Точка сети. Код — из схемы (для привязки при импорте), название — для людей."""

    __tablename__ = "network_node"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[NodeKind] = mapped_column(enum_col(NodeKind, "node_kind"), index=True)
    code: Mapped[str | None] = mapped_column(sa.String(60), index=True)  # «к.№348», «57121», «02-17»
    name: Mapped[str | None] = mapped_column(sa.String(200))
    owner_id: Mapped[int | None] = mapped_column(sa.ForeignKey("owner.id"), index=True)
    geom = mapped_column(point(), nullable=False)
    address: Mapped[str | None] = mapped_column(sa.String(300))
    note: Mapped[str | None] = mapped_column(sa.Text)

    owner: Mapped[Owner | None] = relationship()


class RouteSegment(SourceMixin, TimestampMixin, Base):
    """Участок трассы между двумя узлами: пролёт канализации, пролёт опор, фасад, ГНБ…"""

    __tablename__ = "route_segment"
    __table_args__ = (
        sa.CheckConstraint("node_a_id <> node_b_id", name="distinct_ends"),
        sa.CheckConstraint("fact_length_m IS NULL OR fact_length_m > 0", name="fact_length_positive"),
        # Если фактическая длина задана — обязан быть указан её источник
        sa.CheckConstraint("fact_length_m IS NULL OR length_source IS NOT NULL", name="length_has_source"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[SegmentKind] = mapped_column(enum_col(SegmentKind, "segment_kind"), index=True)
    owner_id: Mapped[int | None] = mapped_column(sa.ForeignKey("owner.id"), index=True)
    node_a_id: Mapped[int] = mapped_column(sa.ForeignKey("network_node.id"), index=True)
    node_b_id: Mapped[int] = mapped_column(sa.ForeignKey("network_node.id"), index=True)
    geom = mapped_column(linestring(), nullable=False)
    geom_length_m: Mapped[float] = mapped_column(sa.Float)  # по геометрии (geography), пересчитывается
    fact_length_m: Mapped[float | None] = mapped_column(sa.Float)
    length_source: Mapped[LengthSourceCode | None] = mapped_column(sa.ForeignKey("length_source.code"))
    length_note: Mapped[str | None] = mapped_column(sa.String(300))
    length_updated_by: Mapped[int | None] = mapped_column(sa.ForeignKey("app_user.id"))
    length_updated_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    channels_total: Mapped[int | None] = mapped_column(sa.SmallInteger)
    name: Mapped[str | None] = mapped_column(sa.String(200))  # исходное название из схемы

    owner: Mapped[Owner | None] = relationship()
    node_a: Mapped[NetworkNode] = relationship(foreign_keys=[node_a_id])
    node_b: Mapped[NetworkNode] = relationship(foreign_keys=[node_b_id])

    @property
    def best_length_m(self) -> float:
        """Лучшая известная длина: фактическая, иначе по геометрии."""
        return self.fact_length_m if self.fact_length_m is not None else self.geom_length_m
