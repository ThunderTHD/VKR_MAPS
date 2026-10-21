"""Импорт KML: загрузка, предпросмотр, изменение сопоставления папок, подтверждение."""
from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_role
from app.core.db import get_db
from app.models import AppUser, ImportJob, Role
from app.services.kml import jobs
from app.services.kml.importer import FolderMapping
from app.services.kml.parser import KmlError

router = APIRouter(prefix="/imports", tags=["Импорт"])
admin_only = require_role(Role.admin)


class MappingIn(BaseModel):
    target: str = Field(pattern="^(node|segment|closure|unclassified|skip)$")
    node_kind: str | None = None
    segment_kind: str | None = None
    owner_id: int | None = None


class PreviewIn(BaseModel):
    mapping: dict[str, MappingIn]
    tolerance_m: float = Field(10.0, ge=0.5, le=200)


def _job(db: Session, job_id: int) -> ImportJob:
    job = db.get(ImportJob, job_id)
    if job is None:
        raise HTTPException(404, "Импорт не найден")
    return job


def _job_info(job: ImportJob) -> dict:
    return {"id": job.id, "kind": job.kind.value, "filename": job.filename, "status": job.status.value,
            "created_at": job.created_at.isoformat() if job.created_at else None,
            "summary": (job.report or job.preview or {}).get("summary")}


@router.post("/kml", summary="Загрузить KML/KMZ и получить предпросмотр")
async def upload_kml(file: UploadFile = File(...), db: Session = Depends(get_db),
                     user: AppUser = Depends(admin_only)) -> dict:
    data = await file.read()
    try:
        job = jobs.create_job(db, file.filename or "import.kml", data, user.id)
        preview = jobs.build_preview(db, job)
    except KmlError as exc:
        db.rollback()
        raise HTTPException(400, str(exc)) from None
    db.commit()
    return {"job": _job_info(job), **preview}


@router.get("", summary="Последние импорты")
def list_jobs(db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> list[dict]:
    return [_job_info(j) for j in jobs.recent_jobs(db)]


@router.get("/{job_id}", summary="Предпросмотр импорта")
def get_preview(job_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    job = _job(db, job_id)
    preview = jobs.build_preview(db, job)
    db.rollback()  # просмотр ничего не меняет
    return {"job": _job_info(job), **preview}


@router.post("/{job_id}/preview", summary="Перестроить предпросмотр с новым сопоставлением папок")
def rebuild_preview(job_id: int, data: PreviewIn, db: Session = Depends(get_db),
                    user: AppUser = Depends(admin_only)) -> dict:
    job = _job(db, job_id)
    mapping = {k: FolderMapping(**v.model_dump()) for k, v in data.mapping.items()}
    preview = jobs.build_preview(db, job, mapping, data.tolerance_m)
    db.commit()
    return {"job": _job_info(job), **preview}


@router.post("/{job_id}/commit", summary="Записать импорт в базу")
def commit(job_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    job = _job(db, job_id)
    try:
        report = jobs.commit_job(db, job, user.id)
    except KmlError as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None
    db.commit()
    return {"job": _job_info(job), "report": report}


@router.post("/{job_id}/discard", summary="Отменить импорт")
def discard(job_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    job = _job(db, job_id)
    jobs.discard_job(db, job)
    db.commit()
    return _job_info(job)

