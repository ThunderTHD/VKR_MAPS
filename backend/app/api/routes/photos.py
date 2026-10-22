"""Точки сети по фотографиям: разбор пачки фото и подтверждение."""
from typing import Literal

from fastapi import APIRouter, Depends, File, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routes.edit import _run
from app.core.auth import require_role
from app.core.db import get_db
from app.models import AppUser, Role
from app.services import documents, photo_points

router = APIRouter(tags=["Фото"])
installer_or_admin = require_role(Role.installer)


class PhotoItem(BaseModel):
    doc_id: int
    action: Literal["attach", "create", "skip"]
    node_id: int | None = None
    kind: str | None = None
    code: str | None = None


class CommitIn(BaseModel):
    items: list[PhotoItem]


@router.post("/photos/analyze", summary="Загрузить фото и найти координаты съёмки (ничего не меняет в сети)")
async def analyze(files: list[UploadFile] = File(...), db: Session = Depends(get_db),
                  user: AppUser = Depends(installer_or_admin)) -> list[dict]:
    files = files[:photo_points.MAX_FILES + 1]   # лишние отсечёт проверка количества
    data = [(f.filename or "фото.jpg", await f.read(documents.MAX_BYTES + 1)) for f in files]
    return _run(db, photo_points.analyze, db, data, user.id)


@router.post("/photos/commit", summary="Прикрепить фото к точкам / создать точки по фото")
def commit(data: CommitIn, db: Session = Depends(get_db), user: AppUser = Depends(installer_or_admin)) -> dict:
    return _run(db, photo_points.commit, db, [i.model_dump() for i in data.items], user.id)
