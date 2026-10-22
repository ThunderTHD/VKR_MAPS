"""Служебные таблицы: пользователи, аудит, документы, импорт, пакеты ИД, подложки."""
from datetime import date, datetime
from enum import StrEnum

import sqlalchemy as sa
from geoalchemy2 import Geometry
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base
from app.models._common import SRID, TimestampMixin, enum_col
from app.models.network import NodeKind, SegmentKind


class Role(StrEnum):
    admin = "admin"          # всё, включая импорт, правку и калибровку
    installer = "installer"  # монтажник: инциденты, фото, длины с пометкой источника
    viewer = "viewer"        # только просмотр


class AppUser(TimestampMixin, Base):
    __tablename__ = "app_user"

    id: Mapped[int] = mapped_column(primary_key=True)
    login: Mapped[str] = mapped_column(sa.String(60), unique=True)
    full_name: Mapped[str] = mapped_column(sa.String(120))
    role: Mapped[Role] = mapped_column(enum_col(Role, "user_role"), default=Role.viewer)
    password_hash: Mapped[str] = mapped_column(sa.String(255))
    active: Mapped[bool] = mapped_column(sa.Boolean, default=True)


class AuditLog(Base):
    """Журнал изменений: кто, когда, что сделал, было → стало."""

    __tablename__ = "audit_log"
    __table_args__ = (sa.Index("ix_audit_log_entity", "entity_type", "entity_id"),)

    id: Mapped[int] = mapped_column(sa.BigInteger, primary_key=True)
    ts: Mapped[datetime] = mapped_column(sa.DateTime(timezone=True), server_default=sa.func.now(), index=True)
    user_id: Mapped[int | None] = mapped_column(sa.ForeignKey("app_user.id", ondelete="SET NULL"))
    action: Mapped[str] = mapped_column(sa.String(40))  # create | update | delete | merge | import | ...
    entity_type: Mapped[str] = mapped_column(sa.String(40))
    entity_id: Mapped[int | None] = mapped_column(sa.Integer)
    before = mapped_column(JSONB, nullable=True)
    after = mapped_column(JSONB, nullable=True)
    comment: Mapped[str | None] = mapped_column(sa.String(300))

    user: Mapped[AppUser | None] = relationship()


class DocumentKind(StrEnum):
    splice_scheme = "splice_scheme"    # схема рассветки
    route_scheme = "route_scheme"      # схема трассы
    passport = "passport"              # паспорт
    act = "act"                        # акт
    asbuilt = "asbuilt"                # пакет ИД целиком
    calibration = "calibration"        # свидетельство о поверке
    photo = "photo"
    other = "other"


class Document(TimestampMixin, Base):
    """Файл, прикреплённый к любому объекту (полиморфная ссылка owner_type + owner_id)."""

    __tablename__ = "document"
    __table_args__ = (sa.Index("ix_document_owner", "owner_type", "owner_id"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    owner_type: Mapped[str | None] = mapped_column(sa.String(40))
    owner_id: Mapped[int | None] = mapped_column(sa.Integer)
    kind: Mapped[DocumentKind] = mapped_column(enum_col(DocumentKind, "document_kind"),
                                               default=DocumentKind.other)
    filename: Mapped[str] = mapped_column(sa.String(255))  # исходное имя
    path: Mapped[str] = mapped_column(sa.String(500))      # путь в хранилище (UPLOAD_DIR)
    mime: Mapped[str | None] = mapped_column(sa.String(100))
    size: Mapped[int | None] = mapped_column(sa.BigInteger)
    sha256: Mapped[str | None] = mapped_column(sa.String(64), index=True)
    uploaded_by: Mapped[int | None] = mapped_column(sa.ForeignKey("app_user.id", ondelete="SET NULL"))
    note: Mapped[str | None] = mapped_column(sa.String(300))


class PackageStatus(StrEnum):
    processing = "processing"  # идёт разбор (в фоне: OCR сканов занимает до минуты)
    failed = "failed"          # файл не удалось разобрать
    parsed = "parsed"        # разобран, ждёт проверки
    confirmed = "confirmed"  # данные перенесены в сеть
    rejected = "rejected"


class AsBuiltPackage(TimestampMixin, Base):
    """Пакет исполнительной документации (PDF): сырой разбор и отчёт проверки."""

    __tablename__ = "asbuilt_package"

    id: Mapped[int] = mapped_column(primary_key=True)
    volp: Mapped[str | None] = mapped_column(sa.String(60), index=True)  # «16000149/03»
    object_name: Mapped[str | None] = mapped_column(sa.String(500))
    contractor: Mapped[str | None] = mapped_column(sa.String(200))
    doc_date: Mapped[date | None] = mapped_column(sa.Date)
    document_id: Mapped[int | None] = mapped_column(sa.ForeignKey("document.id", ondelete="SET NULL"))
    status: Mapped[PackageStatus] = mapped_column(enum_col(PackageStatus, "package_status"),
                                                  default=PackageStatus.parsed)
    parse_result = mapped_column(JSONB, nullable=True)  # как пришло из документа (до подтверждения)
    check_report = mapped_column(JSONB, nullable=True)
    uploaded_by: Mapped[int | None] = mapped_column(sa.ForeignKey("app_user.id", ondelete="SET NULL"))
    filename: Mapped[str | None] = mapped_column(sa.String(255))
    error: Mapped[str | None] = mapped_column(sa.Text)
    cable_id: Mapped[int | None] = mapped_column(sa.ForeignKey("cable.id", ondelete="SET NULL"))

    document: Mapped[Document | None] = relationship()


class ImportKind(StrEnum):
    kml = "kml"
    asbuilt = "asbuilt"
    docx = "docx"
    shp = "shp"


class ImportStatus(StrEnum):
    preview = "preview"      # разобран, показан предпросмотр
    committed = "committed"  # записан в базу
    failed = "failed"
    discarded = "discarded"


class ImportJob(TimestampMixin, Base):
    __tablename__ = "import_job"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[ImportKind] = mapped_column(enum_col(ImportKind, "import_kind"))
    filename: Mapped[str] = mapped_column(sa.String(255))
    document_id: Mapped[int | None] = mapped_column(sa.ForeignKey("document.id", ondelete="SET NULL"))
    user_id: Mapped[int | None] = mapped_column(sa.ForeignKey("app_user.id", ondelete="SET NULL"))
    status: Mapped[ImportStatus] = mapped_column(enum_col(ImportStatus, "import_status"),
                                                 default=ImportStatus.preview)
    folder_mapping = mapped_column(JSONB, nullable=True)
    preview = mapped_column(JSONB, nullable=True)
    report = mapped_column(JSONB, nullable=True)


class FolderTarget(StrEnum):
    node = "node"
    segment = "segment"
    closure = "closure"
    unclassified = "unclassified"
    skip = "skip"


class KmlFolderRule(Base):
    """Запомненное сопоставление папки KML с типом объекта и владельцем."""

    __tablename__ = "kml_folder_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    folder_name: Mapped[str] = mapped_column(sa.String(200), unique=True)
    target: Mapped[FolderTarget] = mapped_column(enum_col(FolderTarget, "folder_target"))
    node_kind: Mapped[NodeKind | None] = mapped_column(enum_col(NodeKind, "rule_node_kind"))
    segment_kind: Mapped[SegmentKind | None] = mapped_column(enum_col(SegmentKind, "rule_segment_kind"))
    owner_id: Mapped[int | None] = mapped_column(sa.ForeignKey("owner.id", ondelete="SET NULL"))


class UnclassifiedFeature(TimestampMixin, Base):
    """Объект импорта без назначенного типа (например, папка «NEC»)."""

    __tablename__ = "unclassified_feature"

    id: Mapped[int] = mapped_column(primary_key=True)
    import_job_id: Mapped[int | None] = mapped_column(sa.ForeignKey("import_job.id", ondelete="SET NULL"))
    folder: Mapped[str | None] = mapped_column(sa.String(300))
    name: Mapped[str | None] = mapped_column(sa.String(300))
    # Любая геометрия: точка, линия, полигон
    geom = mapped_column(Geometry("GEOMETRY", srid=SRID, spatial_index=True), nullable=False)


class MapOverlay(TimestampMixin, Base):
    """Растровая подложка (скан схемы), привязанная к карте по опорным точкам."""

    __tablename__ = "map_overlay"

    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(sa.String(200))
    document_id: Mapped[int | None] = mapped_column(sa.ForeignKey("document.id", ondelete="SET NULL"))
    control_points = mapped_column(JSONB, nullable=False, server_default="[]")  # [{px,py,lat,lon}]
    opacity: Mapped[float] = mapped_column(sa.Float, default=0.6)
    visible: Mapped[bool] = mapped_column(sa.Boolean, default=True)
