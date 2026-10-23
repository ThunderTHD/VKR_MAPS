"""Подложки (сканы планов на карте) и перемычки между объектами."""
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.edit import _get, _run
from app.core.auth import current_user, require_role
from app.core.db import get_db
from app.models import AppUser, Jumper, MapOverlay, Role
from app.services import jumpers, overlays
from app.services.editing import EditError

router = APIRouter(tags=["Подложки и перемычки"])
admin_only = require_role(Role.admin)
installer_or_admin = require_role(Role.installer)


class ControlPoint(BaseModel):
    px: float
    py: float
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


class PointsIn(BaseModel):
    points: list[ControlPoint]


class OverlayPatch(BaseModel):
    name: str | None = None
    opacity: float | None = None
    visible: bool | None = None


class JumperIn(BaseModel):
    a_type: Literal["node", "closure", "cross", "access_node"]
    a_id: int
    b_type: Literal["node", "closure", "cross", "access_node"]
    b_id: int
    label: str | None = Field(None, max_length=200)


class JumperPatch(BaseModel):
    label: str | None = Field(None, max_length=200)


# ------------------------------------------------------------------ подложки


@router.get("/overlays", summary="Подложки", dependencies=[Depends(current_user)])
def list_overlays(db: Session = Depends(get_db)) -> list[dict]:
    return [overlays.summary(o) for o in db.scalars(sa.select(MapOverlay).order_by(MapOverlay.name))]


@router.post("/overlays", summary="Загрузить подложку: картинка или PDF")
async def create_overlay(file: UploadFile = File(...), name: str | None = Form(None), page: int = Form(1),
                         db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    data = await file.read(60 * 1024 * 1024 + 1)
    ov = _run(db, overlays.create, db, filename=file.filename or "план.png", data=data, name=name, page=page,
              user_id=user.id)
    return overlays.summary(ov)


@router.post("/overlays/fit", summary="Проверить привязку (без сохранения): углы и невязки",
             dependencies=[Depends(admin_only)])
def fit(data: PointsIn, size: list[int] = Query(...)) -> dict:
    try:
        return overlays.fit([p.model_dump() for p in data.points], size)
    except EditError as exc:
        raise HTTPException(409, str(exc)) from None


@router.put("/overlays/{ov_id}/points", summary="Сохранить опорные точки привязки")
def set_points(ov_id: int, data: PointsIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    ov = _get(db, MapOverlay, ov_id)
    _run(db, overlays.set_points, db, ov, [p.model_dump() for p in data.points], user.id)
    return overlays.summary(ov)


@router.patch("/overlays/{ov_id}", summary="Название, прозрачность, видимость")
def patch_overlay(ov_id: int, data: OverlayPatch, db: Session = Depends(get_db),
                  user: AppUser = Depends(admin_only)) -> dict:
    ov = _get(db, MapOverlay, ov_id)
    _run(db, overlays.update, db, ov, data.model_dump(exclude_unset=True))
    return overlays.summary(ov)


@router.delete("/overlays/{ov_id}", summary="Удалить подложку")
def delete_overlay(ov_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    _run(db, overlays.delete, db, _get(db, MapOverlay, ov_id), user.id)
    return {"ok": True}


# ------------------------------------------------------------------ перемычки


@router.get("/jumpers/nearest", summary="Ближайший объект для конца перемычки", dependencies=[Depends(current_user)])
def nearest(lon: float, lat: float, within_m: float = Query(25, gt=0, le=200), db: Session = Depends(get_db)):
    return jumpers.nearest(db, lon, lat, within_m)


@router.post("/jumpers", summary="Нарисовать перемычку")
def create_jumper(data: JumperIn, db: Session = Depends(get_db), user: AppUser = Depends(installer_or_admin)) -> dict:
    j = _run(db, jumpers.create, db, **data.model_dump(), user_id=user.id)
    return {"id": j.id}


@router.patch("/jumpers/{j_id}", summary="Изменить подпись перемычки")
def patch_jumper(j_id: int, data: JumperPatch, db: Session = Depends(get_db),
                 user: AppUser = Depends(installer_or_admin)) -> dict:
    _run(db, jumpers.update, db, _get(db, Jumper, j_id), data.label, user.id)
    return {"ok": True}


@router.delete("/jumpers/{j_id}", summary="Удалить перемычку")
def delete_jumper(j_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    _run(db, jumpers.delete, db, _get(db, Jumper, j_id), user.id)
    return {"ok": True}
