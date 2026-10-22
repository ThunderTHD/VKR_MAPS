"""Карта: слои GeoJSON, справочники для легенды и фильтров, поиск, карточки объектов."""
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.core.auth import current_user
from app.core.db import get_db
from app.models import AppUser, CableType, LengthSource, Operator, Owner
from app.services import cards, field
from app.services.map_data import LAYERS, MapFilter, layer_geojson, network_extent

router = APIRouter(tags=["Карта"], dependencies=[Depends(current_user)])

LayerName = Literal["segments", "nodes", "cables", "closures", "access_nodes", "jumpers", "unclassified", "incidents"]
CardType = Literal["node", "segment", "cable", "closure", "cross", "access_node", "unclassified", "incident"]


def _parse_bbox(bbox: str | None) -> tuple[float, float, float, float] | None:
    if not bbox:
        return None
    try:
        x1, y1, x2, y2 = (float(v) for v in bbox.split(","))
    except ValueError:
        raise HTTPException(422, "bbox: ожидается minLon,minLat,maxLon,maxLat") from None
    return x1, y1, x2, y2


@router.get("/map/layers/{layer}", summary="Слой карты в формате GeoJSON")
def get_layer(
    layer: LayerName,
    operator_id: int | None = Query(None, description="только объекты оператора"),
    owner_id: int | None = Query(None, description="только инфраструктура владельца"),
    bbox: str | None = Query(None, description="minLon,minLat,maxLon,maxLat"),
    db: Session = Depends(get_db),
) -> Response:
    assert layer in LAYERS
    flt = MapFilter(operator_id=operator_id, owner_id=owner_id, bbox=_parse_bbox(bbox))
    return Response(layer_geojson(db, layer, flt), media_type="application/geo+json")


@router.get("/nearby", summary="Рядом со мной: точки сети, муфты и повреждения в работе с расстоянием")
def nearby(lon: float = Query(ge=-180, le=180), lat: float = Query(ge=-90, le=90),
           radius: float = Query(300, gt=0, le=5000), db: Session = Depends(get_db)) -> dict:
    return field.nearby(db, lon, lat, radius)


@router.get("/map/reference", summary="Справочники для легенды и фильтров")
def reference(db: Session = Depends(get_db)) -> dict:
    return {
        "operators": [{"id": o.id, "name": o.short_name, "full_name": o.name, "color": o.color}
                      for o in db.scalars(sa.select(Operator).order_by(Operator.id))],
        "owners": [{"id": o.id, "name": o.short_name, "full_name": o.name, "color": o.color}
                   for o in db.scalars(sa.select(Owner).order_by(Owner.id))],
        "length_sources": [{"code": s.code.value, "name": s.name, "error_pct": s.default_error_pct}
                           for s in db.scalars(sa.select(LengthSource).order_by(LengthSource.priority))],
        "cable_types": [{"id": t.id, "mark": t.mark, "fibers": t.fiber_count}
                        for t in db.scalars(sa.select(CableType).order_by(CableType.mark))],
        "node_kinds": cards.NODE_KIND_RU,
        "segment_kinds": cards.SEGMENT_KIND_RU,
        "extent": network_extent(db),
    }


@router.get("/search", summary="Поиск: код/название/адрес узла, муфта, кабель, hostname, IP")
def search(q: str = Query(..., min_length=1), limit: int = Query(20, le=100),
           db: Session = Depends(get_db)) -> list[dict]:
    return cards.search(db, q, limit)


@router.get("/objects/{obj_type}/{obj_id}", summary="Карточка объекта")
def object_card(obj_type: CardType, obj_id: int, db: Session = Depends(get_db),
                user: AppUser = Depends(current_user)) -> dict:
    card = cards.CARDS[obj_type](db, obj_id)
    if card is None:
        raise HTTPException(404, "Объект не найден")
    return card
