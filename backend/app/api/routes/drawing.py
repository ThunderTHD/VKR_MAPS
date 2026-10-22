"""Рисование на карте и прокладка кабелей (только администратор; предпросмотр маршрута — всем вошедшим)."""
from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, model_validator
from sqlalchemy.orm import Session

from app.api.routes.edit import _get, _run
from app.core.auth import current_user, require_role
from app.core.db import get_db
from app.models import AppUser, Cable, Role, RouteSegment
from app.services import drawing, routing
from app.services.editing import EditError

router = APIRouter(tags=["Рисование и кабели"])
admin_only = require_role(Role.admin)

Coord = list[float]


class NodeIn(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    kind: str = "well"
    code: str | None = None
    name: str | None = None
    owner_id: int | None = None
    address: str | None = None
    note: str | None = None


class EndIn(BaseModel):
    """Конец участка: существующая точка (node_id) или координаты (будет привязка или новая точка)."""
    node_id: int | None = None
    lon: float | None = Field(None, ge=-180, le=180)
    lat: float | None = Field(None, ge=-90, le=90)
    kind: str | None = None

    @model_validator(mode="after")
    def _one_of(self):
        if self.node_id is None and (self.lon is None or self.lat is None):
            raise ValueError("Конец участка: нужен node_id или координаты lon/lat")
        return self


class SegmentIn(BaseModel):
    start: EndIn
    end: EndIn
    vertices: list[Coord] = Field(default_factory=list, description="промежуточные изгибы [[lon, lat], …]")
    kind: str = "duct"
    owner_id: int | None = None
    name: str | None = None
    new_node_kind: str = "well"
    fact_length_m: float | None = Field(None, gt=0)
    length_source: str | None = None


class GeometryIn(BaseModel):
    vertices: list[Coord] = Field(description="промежуточные вершины без концов [[lon, lat], …]")


class SplitIn(BaseModel):
    lon: float
    lat: float
    kind: str = "well"


class PathIn(BaseModel):
    waypoints: list[int] = Field(min_length=2, description="id точек по порядку: начало, промежуточные, конец")


class ReserveIn(BaseModel):
    node_id: int
    length_m: float = Field(gt=0)
    source: str | None = "engineer"


class CableIn(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    operator_id: int
    cable_type_id: int
    waypoints: list[int] = Field(min_length=2)
    twist_factor: float = Field(0.005, ge=0, lt=0.2)
    lay_year: int | None = Field(None, ge=1970, le=2100)
    note: str | None = None
    reserves: list[ReserveIn] = Field(default_factory=list)


class CableUpdate(BaseModel):
    name: str | None = None
    operator_id: int | None = None
    twist_factor: float | None = None
    lay_year: int | None = None
    note: str | None = None


def _check_coords(vertices: list[Coord]) -> list[list[float]]:
    for v in vertices:
        if len(v) != 2 or not (-180 <= v[0] <= 180 and -90 <= v[1] <= 90):
            raise HTTPException(422, "Вершины: ожидается [[lon, lat], …]")
    return [[float(v[0]), float(v[1])] for v in vertices]


@router.post("/nodes", summary="Создать точку сети")
def create_node(data: NodeIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    node = _run(db, drawing.create_node, db, **data.model_dump(), user_id=user.id)
    return {"ok": True, "id": node.id}


@router.post("/segments", summary="Нарисовать участок трассы (концы привязываются к точкам)")
def create_segment(data: SegmentIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    payload = data.model_dump()
    payload["vertices"] = _check_coords(data.vertices)
    seg = _run(db, drawing.create_segment, db, **payload, user_id=user.id)
    return {"ok": True, "id": seg.id, "node_a_id": seg.node_a_id, "node_b_id": seg.node_b_id,
            "length_m": round(seg.geom_length_m, 1)}


@router.put("/segments/{seg_id}/geometry", summary="Изменить форму трассы (концы остаются на точках)")
def put_geometry(seg_id: int, data: GeometryIn, db: Session = Depends(get_db),
                 user: AppUser = Depends(admin_only)) -> dict:
    seg = _get(db, RouteSegment, seg_id)
    _run(db, drawing.update_segment_geometry, db, seg, _check_coords(data.vertices), user.id)
    return {"ok": True, "id": seg_id, "length_m": round(seg.geom_length_m, 1)}


@router.post("/segments/{seg_id}/split", summary="Вставить точку в участок (разделить на два)")
def split(seg_id: int, data: SplitIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    seg = _get(db, RouteSegment, seg_id)
    node = _run(db, drawing.split_segment, db, seg, data.lon, data.lat, data.kind, user.id)
    return {"ok": True, "node_id": node.id}


@router.post("/routing/path", summary="Предпросмотр маршрута кабеля через опорные точки",
             dependencies=[Depends(current_user)])
def path(data: PathIn, db: Session = Depends(get_db)) -> dict:
    try:
        plan = routing.find_route(db, data.waypoints)
    except EditError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"segments": [{"id": s.segment_id, "direction": s.direction} for s in plan.steps],
            "nodes": plan.nodes, "length_m": round(plan.length_m, 1),
            "unknown_length_segments": sum(1 for s in plan.steps if not s.known),
            "warnings": routing.check_route_repeats(plan), "geojson": routing.route_geojson(db, plan)}


@router.post("/cables", summary="Проложить кабель по трассе")
def create_cable(data: CableIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    payload = data.model_dump()
    cable = _run(db, routing.create_cable, db, **payload, user_id=user.id)
    return {"ok": True, "id": cable.id}


@router.patch("/cables/{cable_id}", summary="Изменить кабель (название, оператор, укрутка…)")
def patch_cable(cable_id: int, data: CableUpdate, db: Session = Depends(get_db),
                user: AppUser = Depends(admin_only)) -> dict:
    cable = _get(db, Cable, cable_id)
    _run(db, routing.update_cable, db, cable, data.model_dump(exclude_unset=True), user.id)
    return {"ok": True, "id": cable_id}


@router.put("/cables/{cable_id}/route", summary="Перепроложить кабель через новые опорные точки")
def put_route(cable_id: int, data: PathIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    cable = _get(db, Cable, cable_id)
    _run(db, routing.reroute_cable, db, cable, data.waypoints, user.id)
    return {"ok": True, "id": cable_id}


@router.delete("/cables/{cable_id}", summary="Удалить кабель без сварок и портов")
def delete_cable(cable_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    _run(db, routing.delete_cable, db, _get(db, Cable, cable_id), user.id)
    return {"ok": True}
