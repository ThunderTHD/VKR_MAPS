"""Системы координат (МСК и др.): список, добавление (в т. ч. из файла .prj), проверка, удаление."""
import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routes.edit import _get, _run
from app.core.auth import current_user, require_role
from app.core.db import get_db
from app.models import AppUser, CrsDef, ImportJob, Role
from app.services import crs
from app.services.editing import EditError

router = APIRouter(tags=["Системы координат"])
admin_only = require_role(Role.admin)


class CheckIn(BaseModel):
    definition: str


@router.get("/crs", summary="Системы координат", dependencies=[Depends(current_user)])
def list_crs(db: Session = Depends(get_db)) -> list[dict]:
    return crs.list_crs(db)


@router.post("/crs/check", summary="Проверить определение системы координат", dependencies=[Depends(admin_only)])
def check(data: CheckIn, db: Session = Depends(get_db)) -> dict:
    try:
        return crs.check(db, data.definition)
    except EditError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/crs", summary="Добавить систему координат: текст определения или файл .prj")
async def create(name: str | None = Form(None), definition: str | None = Form(None),
                 prj: UploadFile | None = File(None), show_in_cards: bool = Form(True),
                 db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    if prj is not None:
        definition = (await prj.read(200_000)).decode("utf-8", errors="ignore")
    c = _run(db, crs.create, db, name, definition or "", show_in_cards)
    return {"id": c.id, "name": c.name}


@router.delete("/crs/{crs_id}", summary="Удалить систему координат")
def delete(crs_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    c = _get(db, CrsDef, crs_id)
    db.execute(sa.update(ImportJob).where(ImportJob.crs_id == c.id).values(crs_id=None))
    db.delete(c)
    db.commit()
    return {"ok": True}


@router.get("/crs/point", summary="Координаты точки во всех системах", dependencies=[Depends(current_user)])
def point(lon: float, lat: float, db: Session = Depends(get_db)) -> list[dict]:
    return crs.coords_in_all(db, lon, lat)
