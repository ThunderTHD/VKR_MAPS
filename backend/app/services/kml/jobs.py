"""Задания импорта: хранение исходного файла, предпросмотр, подтверждение.

Исходный файл сохраняется в хранилище документов: при подтверждении план строится
заново из того же файла и того же сопоставления папок — так запись всегда соответствует
текущему состоянию базы, а исходник остаётся для истории.
"""
import hashlib
import uuid
from dataclasses import asdict

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, DocumentKind, ImportJob, ImportKind, ImportStatus
from app.services.kml.importer import FolderMapping, commit_plan, plan_import, suggest_mapping
from app.services.kml.parser import KmlError, parse_kml

MAX_KML_BYTES = 50 * 1024 * 1024


def store_upload(db: Session, filename: str, data: bytes, kind: DocumentKind, user_id: int | None) -> Document:
    """Сохранить файл в хранилище и создать запись Document."""
    root = get_settings().upload_dir
    rel = f"{kind.value}/{uuid.uuid4().hex}_{_safe(filename)}"
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    doc = Document(kind=kind, filename=filename, path=rel, size=len(data), uploaded_by=user_id,
                   sha256=hashlib.sha256(data).hexdigest())
    db.add(doc)
    db.flush()
    return doc


def _safe(name: str) -> str:
    """Имя файла без каталогов и опасных символов (защита от «../» в имени)."""
    base = name.replace("\\", "/").rsplit("/", 1)[-1]
    return "".join(ch if ch.isalnum() or ch in "._- " else "_" for ch in base)[:120] or "file"


def read_document(doc: Document) -> bytes:
    return (get_settings().upload_dir / doc.path).read_bytes()


def create_job(db: Session, filename: str, data: bytes, user_id: int | None) -> ImportJob:
    if len(data) > MAX_KML_BYTES:
        raise KmlError("Файл больше 50 МБ")
    kml = parse_kml(data)  # проверка, что файл читается, — до сохранения
    doc = store_upload(db, filename, data, DocumentKind.other, user_id)
    mapping = suggest_mapping(db, kml)
    job = ImportJob(kind=ImportKind.kml, filename=filename, document_id=doc.id, user_id=user_id,
                    status=ImportStatus.preview, folder_mapping={k: asdict(v) for k, v in mapping.items()})
    db.add(job)
    db.flush()
    return job


def _mapping(job: ImportJob) -> dict[str, FolderMapping]:
    return {k: FolderMapping(**v) for k, v in (job.folder_mapping or {}).items()}


def build_preview(db: Session, job: ImportJob, mapping: dict[str, FolderMapping] | None = None,
                  tolerance_m: float | None = None) -> dict:
    """Построить (или перестроить с новым сопоставлением) предпросмотр импорта."""
    if mapping is not None:
        job.folder_mapping = {k: asdict(v) for k, v in mapping.items()}
    tol = tolerance_m if tolerance_m is not None else get_settings().snap_tolerance_m
    kml = parse_kml(read_document(db.get(Document, job.document_id)))
    plan = plan_import(db, kml, _mapping(job), tol)
    preview = {"filename": job.filename, "tolerance_m": tol, "folders": list(kml.folders().values()),
               "mapping": job.folder_mapping, "parse_errors": kml.errors, **plan.to_json(),
               "geojson": plan.to_geojson()}
    job.preview = {"summary": preview["summary"], "tolerance_m": tol}
    db.flush()
    return preview


def commit_job(db: Session, job: ImportJob, user_id: int | None) -> dict:
    if job.status != ImportStatus.preview:
        raise KmlError(f"Импорт уже в состоянии «{job.status.value}»")
    tol = (job.preview or {}).get("tolerance_m", get_settings().snap_tolerance_m)
    kml = parse_kml(read_document(db.get(Document, job.document_id)))
    mapping = _mapping(job)
    plan = plan_import(db, kml, mapping, tol)
    created = commit_plan(db, plan, job.id, user_id, mapping)
    job.status = ImportStatus.committed
    job.report = {"created": created, "summary": plan.summary(),
                  "warnings": [asdict(w) for w in plan.warnings]}
    db.flush()
    return job.report


def discard_job(db: Session, job: ImportJob) -> None:
    if job.status == ImportStatus.preview:
        job.status = ImportStatus.discarded


def recent_jobs(db: Session, limit: int = 20) -> list[ImportJob]:
    return list(db.scalars(sa.select(ImportJob).order_by(ImportJob.id.desc()).limit(limit)))
