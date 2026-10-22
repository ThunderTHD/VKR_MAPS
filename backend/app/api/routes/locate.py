"""Трассировка волокна и локализация повреждения по рефлектометру."""
import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.api.routes.edit import _run
from app.core.auth import current_user
from app.core.db import get_db
from app.models import AppUser, Fiber, Role
from app.services import localization, tracing
from app.services.editing import EditError

router = APIRouter(tags=["Трассировка и локализация"])


class StartIn(BaseModel):
    """Откуда идёт свет: порт кросса (обычный случай) или конец волокна кабеля."""
    port_id: int | None = None
    cable_id: int | None = None
    fiber: int | None = Field(None, ge=1, description="номер волокна в кабеле")
    end: str | None = Field(None, pattern="^(a|b)$", description="a — начало кабеля, b — конец")

    @model_validator(mode="after")
    def _one(self):
        if self.port_id is None and not (self.cable_id and self.fiber and self.end):
            raise ValueError("Укажите порт кросса или кабель, номер волокна и конец")
        return self


class LocateIn(StartIn):
    distance_m: float = Field(gt=0, lt=300_000, description="расстояние до события по рефлектометру")
    launch_m: float = Field(0, ge=0, lt=5000, description="длина измерительного шнура (вычитается)")
    ior_set: float | None = Field(None, gt=1.3, lt=1.6, description="показатель преломления, установленный в приборе")
    ior_fiber: float | None = Field(None, gt=1.3, lt=1.6, description="показатель преломления волокна")
    far_distance_m: float | None = Field(None, gt=0, lt=300_000, description="показание с другого конца тракта")
    save: bool = False
    title: str | None = None


def _trace(db: Session, data: StartIn) -> tuple[tracing.Trace, int, int | None]:
    try:
        if data.port_id is not None:
            fiber, end, port = tracing.start_of_port(db, data.port_id)
            return tracing.trace(db, fiber.id, end, start_port=port), fiber.id, port.id
        fiber_id = db.scalar(sa.select(Fiber.id).where(Fiber.cable_id == data.cable_id, Fiber.number == data.fiber))
        if fiber_id is None:
            raise EditError(f"В кабеле нет волокна №{data.fiber}")
        return tracing.trace(db, fiber_id, data.end), fiber_id, None
    except EditError as exc:
        raise HTTPException(409, str(exc)) from None


@router.post("/trace", summary="Трасса волокна: путь по сваркам, ожидаемые события рефлектограммы",
             dependencies=[Depends(current_user)])
def trace(data: StartIn, db: Session = Depends(get_db)) -> dict:
    tr, _, _ = _trace(db, data)
    return tracing.trace_summary(tr)


@router.post("/locate", summary="Место повреждения по расстоянию с рефлектометра")
def locate(data: LocateIn, db: Session = Depends(get_db), user: AppUser = Depends(current_user)) -> dict:
    tr, fiber_id, port_id = _trace(db, data)
    try:
        result = localization.localize(tr, data.distance_m, launch_m=data.launch_m, ior_set=data.ior_set,
                                       ior_fiber=data.ior_fiber, far_distance_m=data.far_distance_m)
    except EditError as exc:
        raise HTTPException(409, str(exc)) from None
    if data.save:
        if user.role not in (Role.admin, Role.installer):
            raise HTTPException(403, "Сохранять повреждения могут монтажник и администратор")
        inc = _run(db, localization.save_incident, db, result, title=data.title, fiber_id=fiber_id, port_id=port_id,
                   distance_m=data.distance_m, ior=data.ior_set, user_id=user.id)
        result["incident_id"] = inc.id
    return result


@router.get("/crosses", summary="Кроссы для выбора точки измерения", dependencies=[Depends(current_user)])
def crosses(q: str | None = None, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(sa.text("""
        SELECT x.id, x.name, x.port_count, coalesce(n.code, n.name, '#' || n.id) AS node, n.address,
               o.short_name AS operator,
               (SELECT count(*) FROM cross_port p WHERE p.cross_id = x.id AND p.fiber_id IS NOT NULL) AS used
        FROM optical_cross x JOIN network_node n ON n.id = x.node_id LEFT JOIN operator o ON o.id = x.operator_id
        WHERE (CAST(:q AS text) IS NULL OR x.name ILIKE :like OR n.code ILIKE :like OR n.name ILIKE :like
               OR n.address ILIKE :like)
        ORDER BY (n.kind = 'site') DESC, n.code NULLS LAST, x.name LIMIT 50"""),
        {"q": q or None, "like": f"%{q or ''}%"}).mappings().all()
    return [dict(r) for r in rows]


@router.get("/crosses/{cross_id}/ports", summary="Порты кросса с подключёнными волокнами",
            dependencies=[Depends(current_user)])
def cross_ports(cross_id: int, db: Session = Depends(get_db)) -> list[dict]:
    rows = db.execute(sa.text("""
        SELECT p.id, p.port_no, c.name AS cable, f.number AS fiber, f.color
        FROM cross_port p LEFT JOIN fiber f ON f.id = p.fiber_id LEFT JOIN cable c ON c.id = f.cable_id
        WHERE p.cross_id = :x ORDER BY p.port_no"""), {"x": cross_id}).mappings().all()
    return [dict(r) for r in rows]

