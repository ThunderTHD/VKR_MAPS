"""Эксплуатация: узлы доступа и мониторинг, приборы, измерения, повреждения, калибровка."""
from datetime import date, datetime
from enum import StrEnum

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import INET, JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models._common import SourceMixin, TimestampMixin, enum_col, point
from app.models.cable import CrossPort, Fiber
from app.models.network import NetworkNode


class NodeStatus(StrEnum):
    up = "up"
    down = "down"
    unknown = "unknown"


class AccessNode(SourceMixin, TimestampMixin, Base):
    """Активное оборудование (коммутатор узла доступа)."""

    __tablename__ = "access_node"

    id: Mapped[int] = mapped_column(primary_key=True)
    node_id: Mapped[int | None] = mapped_column(sa.ForeignKey("network_node.id"), index=True)
    operator_id: Mapped[int | None] = mapped_column(sa.ForeignKey("operator.id"))
    hostname: Mapped[str | None] = mapped_column(sa.String(120), index=True)
    ip = mapped_column(INET, nullable=True, index=True)
    address: Mapped[str | None] = mapped_column(sa.String(300))
    model: Mapped[str | None] = mapped_column(sa.String(120))
    serial: Mapped[str | None] = mapped_column(sa.String(120))
    uplink_id: Mapped[int | None] = mapped_column(sa.ForeignKey("access_node.id", ondelete="SET NULL"))
    uplink_port: Mapped[str | None] = mapped_column(sa.String(60))
    # Порт кросса, на который заведён линейный тракт этого узла
    cross_port_id: Mapped[int | None] = mapped_column(sa.ForeignKey("cross_port.id", ondelete="SET NULL"))
    status: Mapped[NodeStatus] = mapped_column(enum_col(NodeStatus, "node_status"), default=NodeStatus.unknown)
    fail_count: Mapped[int] = mapped_column(sa.SmallInteger, default=0)
    status_changed_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    last_rtt_ms: Mapped[float | None] = mapped_column(sa.Float)

    node: Mapped[NetworkNode | None] = relationship()
    uplink: Mapped["AccessNode | None"] = relationship(remote_side=[id])
    cross_port: Mapped[CrossPort | None] = relationship()


class NodeStatusHistory(Base):
    __tablename__ = "node_status_history"

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    access_node_id: Mapped[int] = mapped_column(sa.ForeignKey("access_node.id", ondelete="CASCADE"))
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    status: Mapped[NodeStatus] = mapped_column(enum_col(NodeStatus, "node_status_hist"))
    rtt_ms: Mapped[float | None] = mapped_column(sa.Float)

    __table_args__ = (sa.Index("ix_node_status_history_node_ts", "access_node_id", "ts"),)


class InstrumentKind(StrEnum):
    otdr = "otdr"                # рефлектометр
    power_meter = "power_meter"  # оптический тестер
    splicer = "splicer"          # сварочный аппарат
    megohmmeter = "megohmmeter"


class Instrument(Base):
    __tablename__ = "instrument"
    __table_args__ = (sa.UniqueConstraint("model", "serial"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[InstrumentKind] = mapped_column(enum_col(InstrumentKind, "instrument_kind"))
    model: Mapped[str] = mapped_column(sa.String(120))
    registry_no: Mapped[str | None] = mapped_column(sa.String(30))   # номер в Госреестре СИ
    serial: Mapped[str | None] = mapped_column(sa.String(60))        # заводской номер
    calibration_no: Mapped[str | None] = mapped_column(sa.String(80))
    calibration_valid_until: Mapped[date | None] = mapped_column(sa.Date)
    calibration_doc_id: Mapped[int | None] = mapped_column(sa.ForeignKey("document.id", ondelete="SET NULL"))


class MeasurementKind(StrEnum):
    otdr = "otdr"
    power_meter = "power_meter"


class Measurement(SourceMixin, Base):
    """Измерение оптического тракта между двумя портами кроссов (эталон для калибровки)."""

    __tablename__ = "measurement"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[MeasurementKind] = mapped_column(enum_col(MeasurementKind, "measurement_kind"))
    port_a_id: Mapped[int | None] = mapped_column(sa.ForeignKey("cross_port.id", ondelete="SET NULL"))
    port_b_id: Mapped[int | None] = mapped_column(sa.ForeignKey("cross_port.id", ondelete="SET NULL"))
    wavelength_nm: Mapped[int | None] = mapped_column(sa.SmallInteger)
    refractive_index: Mapped[float | None] = mapped_column(sa.Float)
    optical_length_m: Mapped[float | None] = mapped_column(sa.Float)
    loss_ab_db: Mapped[float | None] = mapped_column(sa.Float)
    loss_ba_db: Mapped[float | None] = mapped_column(sa.Float)
    instrument_id: Mapped[int | None] = mapped_column(sa.ForeignKey("instrument.id"))
    measured_at: Mapped[date | None] = mapped_column(sa.Date)
    package_id: Mapped[int | None] = mapped_column(sa.ForeignKey("asbuilt_package.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(sa.Text)

    port_a: Mapped[CrossPort | None] = relationship(foreign_keys=[port_a_id])
    port_b: Mapped[CrossPort | None] = relationship(foreign_keys=[port_b_id])
    instrument: Mapped[Instrument | None] = relationship()


class IncidentStatus(StrEnum):
    open = "open"            # обнаружено, идёт локализация
    localized = "localized"  # место предсказано
    repaired = "repaired"    # восстановлено, фактическое место отмечено
    cancelled = "cancelled"


class Incident(SourceMixin, TimestampMixin, Base):
    """Повреждение: прогноз места по рефлектометру и фактическое место после ремонта."""

    __tablename__ = "incident"

    id: Mapped[int] = mapped_column(primary_key=True)
    status: Mapped[IncidentStatus] = mapped_column(enum_col(IncidentStatus, "incident_status"),
                                                   default=IncidentStatus.open)
    title: Mapped[str | None] = mapped_column(sa.String(200))
    fiber_id: Mapped[int | None] = mapped_column(sa.ForeignKey("fiber.id", ondelete="SET NULL"))
    measure_port_id: Mapped[int | None] = mapped_column(sa.ForeignKey("cross_port.id", ondelete="SET NULL"))
    otdr_distance_m: Mapped[float | None] = mapped_column(sa.Float)
    refractive_index: Mapped[float | None] = mapped_column(sa.Float)
    predicted_geom = mapped_column(point(), nullable=True)
    predicted_error_m: Mapped[float | None] = mapped_column(sa.Float)   # полуширина интервала
    predicted_segment_id: Mapped[int | None] = mapped_column(sa.ForeignKey("route_segment.id"))
    actual_geom = mapped_column(point(), nullable=True)
    error_m: Mapped[float | None] = mapped_column(sa.Float)             # расстояние факт ↔ прогноз
    detected_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    arrived_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    restored_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))
    executor_id: Mapped[int | None] = mapped_column(sa.ForeignKey("app_user.id"))
    calc_details = mapped_column(JSONB, nullable=True)  # разбор расчёта: участки, вклад в погрешность
    note: Mapped[str | None] = mapped_column(sa.Text)

    fiber: Mapped[Fiber | None] = relationship()


class CalibrationRun(Base):
    """Прогон калибровки: какие поправки предложены, метрики до/после, кто утвердил."""

    __tablename__ = "calibration_run"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_at: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now())
    params = mapped_column(JSONB, nullable=False, server_default="{}")
    proposals = mapped_column(JSONB, nullable=False, server_default="[]")
    metrics_before = mapped_column(JSONB, nullable=True)
    metrics_after = mapped_column(JSONB, nullable=True)
    approved_by: Mapped[int | None] = mapped_column(sa.ForeignKey("app_user.id"))
    applied_at: Mapped[datetime | None] = mapped_column(sa.DateTime(timezone=True))


class SimTruth(Base):
    """«Истинные» значения для синтетической сети (генератор).

    Генератор знает настоящую длину каждого участка, а в рабочие поля пишет её с
    ошибкой, соответствующей источнику. Таблица нужна только для эксперимента:
    оценить точность локализации и эффект калибровки. Приложение её не читает.
    """

    __tablename__ = "sim_truth"
    __table_args__ = (sa.UniqueConstraint("entity_type", "entity_id", "attribute"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    entity_type: Mapped[str] = mapped_column(sa.String(40))
    entity_id: Mapped[int] = mapped_column(sa.Integer)
    attribute: Mapped[str] = mapped_column(sa.String(40))
    value: Mapped[float] = mapped_column(sa.Float)
