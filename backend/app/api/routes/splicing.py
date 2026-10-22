"""Муфты, рассветка, кроссы, документы.

Права: смотреть — все вошедшие; сварки и порты — монтажник и администратор (это полевая
работа); создание и удаление муфт и кроссов, разрезание кабеля — администратор.
"""
from typing import Literal
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, Form, HTTPException, Response, UploadFile
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.edit import _get, _run
from app.core.auth import current_user, require_role
from app.core.db import get_db
from app.models import AppUser, Cable, Closure, Document, OpticalCross, Role, Splice
from app.services import documents, splicing

router = APIRouter(tags=["Муфты, кроссы, документы"])
admin_only = require_role(Role.admin)
installer_or_admin = require_role(Role.installer)

End = Literal["a", "b"]


class ClosureIn(BaseModel):
    node_id: int
    number: str = Field(min_length=1, max_length=60)
    model: str | None = None
    operator_id: int | None = None
    note: str | None = None


class ClosureUpdate(BaseModel):
    number: str | None = None
    model: str | None = None
    operator_id: int | None = None
    note: str | None = None


class SpliceIn(BaseModel):
    fiber_a_id: int
    fiber_b_id: int
    end_a: End | None = None
    end_b: End | None = None
    loss_db: float | None = None
    note: str | None = None


class SpliceRangeIn(BaseModel):
    cable_a_id: int
    from_a: int = Field(ge=1)
    cable_b_id: int
    from_b: int = Field(ge=1)
    count: int = Field(ge=1, le=288)
    end_a: End | None = None
    end_b: End | None = None
    note: str | None = None


class SpliceUpdate(BaseModel):
    loss_splicer_db: float | None = None
    loss_otdr_ab_db: float | None = None
    loss_otdr_ba_db: float | None = None
    note: str | None = None


class SplitIn(BaseModel):
    cable_id: int
    new_name: str | None = None
    transit: bool = True


class CrossIn(BaseModel):
    node_id: int
    name: str = Field(min_length=1, max_length=60)
    port_count: int = Field(ge=1, le=288)
    model: str | None = None
    connector: str | None = None
    operator_id: int | None = None


class CrossUpdate(BaseModel):
    name: str | None = None
    model: str | None = None
    connector: str | None = None
    operator_id: int | None = None
    port_count: int | None = None


class ConnectIn(BaseModel):
    cable_id: int
    fiber_from: int = Field(ge=1)
    port_from: int = Field(ge=1)
    count: int = Field(1, ge=1, le=288)
    end: End | None = None


class DisconnectIn(BaseModel):
    port_from: int = Field(ge=1)
    count: int = Field(1, ge=1, le=288)


# ------------------------------------------------------------------ муфты


@router.post("/closures", summary="Поставить муфту в узле")
def create_closure(data: ClosureIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    clo = _run(db, splicing.create_closure, db, **data.model_dump(), user_id=user.id)
    return {"ok": True, "id": clo.id}


@router.patch("/closures/{closure_id}", summary="Изменить муфту")
def patch_closure(closure_id: int, data: ClosureUpdate, db: Session = Depends(get_db),
                  user: AppUser = Depends(admin_only)) -> dict:
    _run(db, splicing.update_closure, db, _get(db, Closure, closure_id), data.model_dump(exclude_unset=True), user.id)
    return {"ok": True}


@router.delete("/closures/{closure_id}", summary="Удалить муфту вместе со сварками")
def delete_closure(closure_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    n = _run(db, splicing.delete_closure, db, _get(db, Closure, closure_id), user.id)
    return {"ok": True, "splices_deleted": n}


@router.get("/closures/{closure_id}/sheet", summary="Рассветка муфты: кабели, волокна, сварки, замечания",
            dependencies=[Depends(current_user)])
def closure_sheet(closure_id: int, db: Session = Depends(get_db)) -> dict:
    return splicing.closure_sheet(db, _get(db, Closure, closure_id))


@router.get("/closures/{closure_id}/sheet.csv", summary="Рассветка в CSV (для Excel)",
            dependencies=[Depends(current_user)])
def closure_csv(closure_id: int, db: Session = Depends(get_db)) -> Response:
    clo = _get(db, Closure, closure_id)
    data = documents.closure_csv(splicing.closure_sheet(db, clo))
    fname = f"rassvetka_{clo.id}.csv"
    return Response(data, media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="{fname}"'})


@router.post("/closures/{closure_id}/splices", summary="Сварить два волокна")
def add_splice(closure_id: int, data: SpliceIn, db: Session = Depends(get_db),
               user: AppUser = Depends(installer_or_admin)) -> dict:
    sp = _run(db, splicing.add_splice, db, _get(db, Closure, closure_id), data.fiber_a_id, data.fiber_b_id,
              end_a=data.end_a, end_b=data.end_b, loss_db=data.loss_db, note=data.note, user_id=user.id)
    return {"ok": True, "id": sp.id}


@router.post("/closures/{closure_id}/splices/range", summary="Сварить волокна подряд")
def splice_range(closure_id: int, data: SpliceRangeIn, db: Session = Depends(get_db),
                 user: AppUser = Depends(installer_or_admin)) -> dict:
    n = _run(db, splicing.splice_range, db, _get(db, Closure, closure_id), **data.model_dump(), user_id=user.id)
    return {"ok": True, "count": n}


@router.post("/closures/{closure_id}/split-cable", summary="Разрезать транзитный кабель в муфте")
def split_cable(closure_id: int, data: SplitIn, db: Session = Depends(get_db),
                user: AppUser = Depends(admin_only)) -> dict:
    clo = _get(db, Closure, closure_id)
    new = _run(db, splicing.split_cable, db, _get(db, Cable, data.cable_id), clo, new_name=data.new_name,
               transit=data.transit, user_id=user.id)
    return {"ok": True, "new_cable_id": new.id}


@router.patch("/splices/{splice_id}", summary="Изменить потери и примечание сварки")
def patch_splice(splice_id: int, data: SpliceUpdate, db: Session = Depends(get_db),
                 user: AppUser = Depends(installer_or_admin)) -> dict:
    _run(db, splicing.update_splice, db, _get(db, Splice, splice_id), data.model_dump(exclude_unset=True), user.id)
    return {"ok": True}


@router.delete("/splices/{splice_id}", summary="Удалить сварку")
def delete_splice(splice_id: int, db: Session = Depends(get_db), user: AppUser = Depends(installer_or_admin)) -> dict:
    _run(db, splicing.delete_splice, db, _get(db, Splice, splice_id), user.id)
    return {"ok": True}


# ------------------------------------------------------------------ кроссы


@router.post("/crosses", summary="Поставить оптический кросс в узле")
def create_cross(data: CrossIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    x = _run(db, splicing.create_cross, db, **data.model_dump(), user_id=user.id)
    return {"ok": True, "id": x.id}


@router.patch("/crosses/{cross_id}", summary="Изменить кросс (в т. ч. число портов)")
def patch_cross(cross_id: int, data: CrossUpdate, db: Session = Depends(get_db),
                user: AppUser = Depends(admin_only)) -> dict:
    _run(db, splicing.update_cross, db, _get(db, OpticalCross, cross_id), data.model_dump(exclude_unset=True),
         user.id)
    return {"ok": True}


@router.delete("/crosses/{cross_id}", summary="Удалить кросс")
def delete_cross(cross_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    n = _run(db, splicing.delete_cross, db, _get(db, OpticalCross, cross_id), user.id)
    return {"ok": True, "ports_disconnected": n}


@router.get("/crosses/{cross_id}/sheet", summary="Порты кросса и кабели в узле", dependencies=[Depends(current_user)])
def cross_sheet(cross_id: int, db: Session = Depends(get_db)) -> dict:
    return splicing.cross_sheet(db, _get(db, OpticalCross, cross_id))


@router.post("/crosses/{cross_id}/connect", summary="Завести волокна на порты подряд")
def connect(cross_id: int, data: ConnectIn, db: Session = Depends(get_db),
            user: AppUser = Depends(installer_or_admin)) -> dict:
    n = _run(db, splicing.connect_ports, db, _get(db, OpticalCross, cross_id), **data.model_dump(), user_id=user.id)
    return {"ok": True, "count": n}


@router.post("/crosses/{cross_id}/disconnect", summary="Отключить порты")
def disconnect(cross_id: int, data: DisconnectIn, db: Session = Depends(get_db),
               user: AppUser = Depends(installer_or_admin)) -> dict:
    n = _run(db, splicing.disconnect_ports, db, _get(db, OpticalCross, cross_id), data.port_from, data.count,
             user.id)
    return {"ok": True, "count": n}


# ------------------------------------------------------------------ документы

OwnerType = Literal["node", "segment", "cable", "closure", "cross", "access_node"]


@router.get("/documents", summary="Документы объекта", dependencies=[Depends(current_user)])
def list_docs(owner_type: OwnerType, owner_id: int, db: Session = Depends(get_db)) -> list[dict]:
    return documents.list_documents(db, owner_type, owner_id)


@router.post("/documents", summary="Прикрепить документ к объекту")
async def upload_doc(owner_type: OwnerType = Form(...), owner_id: int = Form(...), kind: str = Form("other"),
                     note: str | None = Form(None), file: UploadFile = File(...), db: Session = Depends(get_db),
                     user: AppUser = Depends(installer_or_admin)) -> dict:
    data = await file.read(documents.MAX_BYTES + 1)
    doc = _run(db, documents.attach, db, owner_type=owner_type, owner_id=owner_id, filename=file.filename or "file",
               data=data, kind=kind, note=note, user_id=user.id)
    return {"ok": True, "id": doc.id}


@router.get("/documents/{doc_id}/file", summary="Скачать или открыть документ", dependencies=[Depends(current_user)])
def get_doc(doc_id: int, download: bool = False, db: Session = Depends(get_db)) -> Response:
    doc = _get(db, Document, doc_id)
    try:
        data, mime, inline = documents.open_document(doc)
    except FileNotFoundError:
        raise HTTPException(404, "Файл отсутствует в хранилище") from None
    disp = "inline" if inline and not download else "attachment"
    # RFC 5987: русское имя файла корректно сохранится в любом браузере
    headers = {"Content-Disposition": f"{disp}; filename=\"document{doc.id}\"; filename*=UTF-8''{quote(doc.filename)}",
               "X-Content-Type-Options": "nosniff", "Cache-Control": "private, max-age=3600"}
    return Response(data, media_type=mime, headers=headers)


@router.delete("/documents/{doc_id}", summary="Удалить документ")
def delete_doc(doc_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    _run(db, documents.delete_document, db, _get(db, Document, doc_id), user.id)
    return {"ok": True}
