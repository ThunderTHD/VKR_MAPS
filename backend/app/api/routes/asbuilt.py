"""Пакеты исполнительной документации: загрузка, ход разбора, результат, сверка, подтверждение."""
import sqlalchemy as sa
from fastapi import APIRouter, Depends, File, Form, HTTPException, UploadFile
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routes.edit import _get, _run
from app.core.auth import require_role
from app.core.db import SessionLocal, get_db
from app.models import AppUser, AsBuiltPackage, Role
from app.services.asbuilt import apply
from app.services.editing import EditError

router = APIRouter(tags=["Пакеты ИД"])
installer_or_admin = require_role(Role.installer)
admin_only = require_role(Role.admin)


class LinkIn(BaseModel):
    cable_id: int | None = None
    port_a_id: int | None = None
    port_b_id: int | None = None


class ConfirmIn(LinkIn):
    measurement: bool = True
    twist: bool = True
    instruments: bool = True
    access_node: bool = True
    poles: bool = False
    attach: bool = True


class RejectIn(BaseModel):
    reason: str | None = None


@router.get("/asbuilt", summary="Пакеты ИД", dependencies=[Depends(installer_or_admin)])
def list_packages(db: Session = Depends(get_db)) -> list[dict]:
    apply.mark_stale(db)
    db.commit()
    rows = db.scalars(sa.select(AsBuiltPackage).order_by(AsBuiltPackage.id.desc()).limit(100)).all()
    return [apply.package_summary(p) for p in rows]


@router.post("/asbuilt", summary="Загрузить пакет ИД (PDF) — разбор идёт в фоне")
async def upload(file: UploadFile = File(...), ocr: bool = Form(True), db: Session = Depends(get_db),
                 user: AppUser = Depends(admin_only)) -> dict:
    data = await file.read(apply.MAX_BYTES + 1)
    pkg = _run(db, apply.create_package, db, file.filename or "ИД.pdf", data, user.id)
    apply.start_processing(SessionLocal, pkg.id, ocr=ocr)
    return apply.package_summary(pkg)


@router.get("/asbuilt/{pkg_id}", summary="Результат разбора и предложения по сопоставлению",
            dependencies=[Depends(installer_or_admin)])
def get_package(pkg_id: int, db: Session = Depends(get_db)) -> dict:
    pkg = _get(db, AsBuiltPackage, pkg_id)
    out = apply.package_summary(pkg)
    if pkg.status.value in ("parsed", "confirmed", "rejected") and pkg.parse_result:
        res = pkg.parse_result
        out.update({"summary": res.get("summary"), "pages": res.get("pages"), "trace": res.get("trace"),
                    "findings": res.get("findings"), "extracted": res.get("extracted"),
                    "check_report": pkg.check_report, "suggest": apply.suggest(db, pkg)})
    return out


@router.post("/asbuilt/{pkg_id}/compare", summary="Сверить пакет с данными системы",
             dependencies=[Depends(installer_or_admin)])
def compare(pkg_id: int, data: LinkIn, db: Session = Depends(get_db)) -> list[dict]:
    try:
        return apply.compare(db, _get(db, AsBuiltPackage, pkg_id), data.cable_id, data.port_a_id, data.port_b_id)
    except EditError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/asbuilt/{pkg_id}/confirm", summary="Подтвердить пакет: записать данные в сеть")
def confirm(pkg_id: int, data: ConfirmIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    pkg = _get(db, AsBuiltPackage, pkg_id)
    return _run(db, apply.confirm, db, pkg, **data.model_dump(), user_id=user.id)


@router.post("/asbuilt/{pkg_id}/reject", summary="Отклонить пакет (вернуть подрядчику)")
def reject(pkg_id: int, data: RejectIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    _run(db, apply.reject, db, _get(db, AsBuiltPackage, pkg_id), data.reason, user.id)
    return {"ok": True}
