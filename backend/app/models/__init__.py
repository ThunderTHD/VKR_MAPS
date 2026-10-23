"""ORM-модели. Все модели импортируются здесь, чтобы Alembic видел все таблицы."""
from app.core.db import Base
from app.models._common import LengthSourceCode, SourceKind
from app.models.cable import (
    Cable,
    CableReserve,
    CableRoute,
    Closure,
    CrossPort,
    Fiber,
    FiberEnd,
    Jumper,
    OpticalCross,
    Splice,
)
from app.models.network import NetworkNode, NodeKind, RouteSegment, SegmentKind
from app.models.operations import (
    AccessNode,
    CalibrationRun,
    ExperimentRun,
    Incident,
    IncidentStatus,
    Instrument,
    InstrumentKind,
    Measurement,
    MeasurementKind,
    NodeStatus,
    NodeStatusHistory,
    SimFault,
    SimTruth,
)
from app.models.reference import CableType, ColorKind, ColorScheme, ColorSchemeEntry, LengthSource, Operator, Owner
from app.models.system import (
    AppUser,
    AsBuiltPackage,
    AuditLog,
    CrsDef,
    Document,
    DocumentKind,
    FolderTarget,
    ImportJob,
    ImportKind,
    ImportStatus,
    KmlFolderRule,
    MapOverlay,
    PackageStatus,
    Role,
    UnclassifiedFeature,
)

__all__ = [
    "AccessNode", "AppUser", "AsBuiltPackage", "AuditLog", "Base", "Cable", "CableReserve", "CableRoute",
    "CableType", "CalibrationRun", "Closure", "ColorKind", "ColorScheme", "ColorSchemeEntry", "CrossPort", "CrsDef",
    "Document", "DocumentKind", "ExperimentRun", "Fiber", "FiberEnd", "FolderTarget", "ImportJob", "ImportKind",
    "ImportStatus",
    "Incident", "IncidentStatus", "Instrument", "InstrumentKind", "Jumper", "KmlFolderRule", "LengthSource",
    "LengthSourceCode", "MapOverlay", "Measurement", "MeasurementKind", "NetworkNode", "NodeKind", "NodeStatus",
    "NodeStatusHistory", "OpticalCross", "Operator", "Owner", "PackageStatus", "Role", "RouteSegment",
    "SegmentKind", "SimFault", "SimTruth", "SourceKind", "Splice", "UnclassifiedFeature",
]
