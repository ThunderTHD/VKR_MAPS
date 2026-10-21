"""Редактирование: узлы, участки, неклассифицированные объекты, журнал изменений."""
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import current_user, require_role
from app.core.db import get_db
from app.models import AppUser, AuditLog, NetworkNode, Role, RouteSegment, UnclassifiedFeature
from app.services import editing
from app.services.editing import EditError

router = APIRouter(tags=["Редактирование"])
admin_only = require_role(Role.admin)
installer_or_admin = require_role(Role.installer)

# Монтажник может уточнять длину участка в поле — остальное только администратор
INSTALLER_SEGMENT_FIELDS = {"fact_length_m", "length_source", "length_note"}


class NodeUpdate(BaseModel):
    kind: str | None = None
    code: str | None = None
    name: str | None = None
    owner_id: int | None = None
    address: str | None = None
    note: str | None = None


class MoveIn(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)


class MergeIn(BaseModel):
    keep_id: int
    remove_id: int


class SegmentUpdate(BaseModel):
    kind: str | None = None
    owner_id: int | None = None
    name: str | None = None
    fact_length_m: float | None = None
    length_source: str | None = None
    length_note: str | None = None
    channels_total: int | None = None


class ClassifyIn(BaseModel):
    target: str = Field(pattern="^(node|segment)$")
    node_kind: str | None = None
    segment_kind: str | None = None
    owner_id: int | None = None


def _get(db: Session, model, obj_id: int):
    obj = db.get(model, obj_id)
    if obj is None:
        raise HTTPException(404, "Объект не найден")
    return obj


def _run(db: Session, fn, *args, **kwargs):
    """Выполнить операцию в транзакции; ошибка предметной области — 409 с понятным текстом."""
    try:
        result = fn(*args, **kwargs)
    except (EditError, ValueError) as exc:
        db.rollback()
        raise HTTPException(409, str(exc)) from None
    db.commit()
    return result


@router.patch("/nodes/{node_id}", summary="Изменить узел (код, название, тип, владелец…)")
def patch_node(node_id: int, data: NodeUpdate, db: Session = Depends(get_db),
               user: AppUser = Depends(admin_only)) -> dict:
    node = _get(db, NetworkNode, node_id)
    _run(db, editing.update_node, db, node, data.model_dump(exclude_unset=True), user.id)
    return {"ok": True, "id": node.id}


@router.post("/nodes/{node_id}/move", summary="Перенести узел (концы участков переносятся вместе с ним)")
def move_node(node_id: int, data: MoveIn, db: Session = Depends(get_db),
              user: AppUser = Depends(admin_only)) -> dict:
    node = _get(db, NetworkNode, node_id)
    _run(db, editing.move_node, db, node, data.lon, data.lat, user.id)
    return {"ok": True, "id": node_id}


@router.post("/nodes/merge", summary="Объединить дубли узлов")
def merge_nodes(data: MergeIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    keep, remove = _get(db, NetworkNode, data.keep_id), _get(db, NetworkNode, data.remove_id)
    return _run(db, editing.merge_nodes, db, keep, remove, user.id)


@router.delete("/nodes/{node_id}", summary="Удалить неиспользуемый узел")
def delete_node(node_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    _run(db, editing.delete_node, db, _get(db, NetworkNode, node_id), user.id)
    return {"ok": True}


@router.patch("/segments/{seg_id}", summary="Изменить участок (монтажнику — только длина и её источник)")
def patch_segment(seg_id: int, data: SegmentUpdate, db: Session = Depends(get_db),
                  user: AppUser = Depends(installer_or_admin)) -> dict:
    seg = _get(db, RouteSegment, seg_id)
    allowed = None if user.role == Role.admin else INSTALLER_SEGMENT_FIELDS
    changes = data.model_dump(exclude_unset=True)
    if allowed is not None and set(changes) - allowed:
        raise HTTPException(403, "Монтажник может менять только длину участка и её источник")
    _run(db, editing.update_segment, db, seg, changes, user.id, allowed)
    return {"ok": True, "id": seg.id}


@router.delete("/segments/{seg_id}", summary="Удалить участок без кабелей")
def delete_segment(seg_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    _run(db, editing.delete_segment, db, _get(db, RouteSegment, seg_id), user.id)
    return {"ok": True}


@router.post("/unclassified/{feat_id}/classify", summary="Назначить тип неклассифицированному объекту")
def classify(feat_id: int, data: ClassifyIn, db: Session = Depends(get_db),
             user: AppUser = Depends(admin_only)) -> dict:
    feat = _get(db, UnclassifiedFeature, feat_id)
    return _run(db, editing.classify_feature, db, feat, data.target, user.id,
                node_kind=data.node_kind, segment_kind=data.segment_kind, owner_id=data.owner_id)


@router.get("/audit", summary="Журнал изменений")
def audit_log(entity_type: str | None = None, entity_id: int | None = None, limit: int = 50,
              db: Session = Depends(get_db), user: AppUser = Depends(current_user)) -> list[dict]:
    q = sa.select(AuditLog, AppUser.full_name).outerjoin(AppUser, AppUser.id == AuditLog.user_id)
    if entity_type:
        q = q.where(AuditLog.entity_type == entity_type)
    if entity_id is not None:
        q = q.where(AuditLog.entity_id == entity_id)
    rows = db.execute(q.order_by(AuditLog.id.desc()).limit(min(limit, 500))).all()
    return [{"id": a.id, "ts": a.ts.isoformat(), "user": name, "action": a.action, "entity_type": a.entity_type,
             "entity_id": a.entity_id, "before": a.before, "after": a.after, "comment": a.comment}
            for a, name in rows]
