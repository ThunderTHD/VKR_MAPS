"""Мониторинг узлов доступа, анализ последствий, корреляция аварий, экспорт сети."""
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.edit import _run
from app.core.auth import current_user, require_role
from app.core.db import get_db
from app.models import AppUser, CrsDef, Role
from app.services import export, impact, monitoring
from app.services.editing import EditError, audit

router = APIRouter(tags=["Мониторинг"], dependencies=[Depends(current_user)])
installer_or_admin = require_role(Role.installer)
admin_only = require_role(Role.admin)


class SimulateIn(BaseModel):
    segment_id: int | None = None
    access_node_id: int | None = None
    minutes: int | None = Field(None, ge=1, le=24 * 60)
    random_cut: bool = False


def _domain(fn, *args, **kwargs):
    try:
        return fn(*args, **kwargs)
    except EditError as exc:
        raise HTTPException(409, str(exc)) from None


@router.get("/monitoring", summary="Состояние узлов доступа, последние события, имитированные аварии")
def get_overview(db: Session = Depends(get_db)) -> dict:
    return monitoring.overview(db)


@router.post("/monitoring/poll", summary="Опросить узлы сейчас")
def poll_now(db: Session = Depends(get_db), user: AppUser = Depends(installer_or_admin)) -> dict:
    return _run(db, monitoring.poll, db)


@router.post("/monitoring/simulate", summary="Имитировать аварию: обрыв участка или отключение узла")
def simulate(data: SimulateIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    def go():
        monitoring.simulate(db, **data.model_dump())
        res = monitoring.poll_until_settled(db)
        return {"poll": res, "correlation": impact.correlate(db)}
    return _run(db, go)


@router.delete("/monitoring/simulate", summary="Снять все имитированные аварии")
def clear(db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    def go():
        n = monitoring.clear_simulation(db)
        return {"cleared": n, "poll": monitoring.poll_until_settled(db)}
    return _run(db, go)


@router.get("/monitoring/correlate", summary="Где вероятнее всего обрыв (по недоступным узлам)")
def correlate(db: Session = Depends(get_db)) -> dict:
    return impact.correlate(db)


@router.get("/monitoring/nodes/{an_id}/availability", summary="Доступность узла за период")
def availability(an_id: int, hours: int = Query(24, ge=1, le=24 * 90), db: Session = Depends(get_db)) -> dict:
    return _domain(monitoring.availability, db, an_id, hours)


@router.get("/impact/{kind}/{obj_id}", summary="Что отключится при повреждении объекта")
def get_impact(kind: str, obj_id: int, db: Session = Depends(get_db)) -> dict:
    return _domain(impact.impact, db, kind, obj_id)


@router.get("/access-nodes/{an_id}/path", summary="Линейный тракт узла доступа (участки, муфты, кабели)")
def access_path(an_id: int, db: Session = Depends(get_db)) -> dict:
    return _domain(impact.path_of, db, an_id)


# ------------------------------------------------------------------ экспорт сети


@router.get("/export/network.{fmt}", summary="Экспорт сети: KML (Google Earth), GeoJSON или Shapefile (zip)")
def export_network(fmt: str, operator_id: int | None = None, crs_id: int | None = None,
                   db: Session = Depends(get_db), user: AppUser = Depends(admin_only)):
    if fmt not in ("kml", "geojson", "zip"):
        raise HTTPException(404, "Формат: kml, geojson или zip (Shapefile)")
    if fmt == "zip":
        crs_def = db.get(CrsDef, crs_id) if crs_id else None
        if crs_id and crs_def is None:
            raise HTTPException(404, "Система координат не найдена")
        try:
            data = export.to_shapefile(db, operator_id, crs_def)
        except EditError as exc:
            raise HTTPException(409, str(exc)) from None
    else:
        data = export.to_kml(db, operator_id) if fmt == "kml" else export.to_geojson(db, operator_id)
    audit(db, user.id, "export", "network", operator_id, None, {"format": fmt, "bytes": len(data), "crs_id": crs_id})
    db.commit()
    mime = {"kml": "application/vnd.google-earth.kml+xml", "geojson": "application/geo+json",
            "zip": "application/zip"}[fmt]
    suffix = f"_оператор{operator_id}" if operator_id else ""
    name = quote(f"сеть_ВОЛС{suffix}{'_shp' if fmt == 'zip' else ''}.{fmt}")
    return Response(data, media_type=mime, headers={"Content-Disposition": f"attachment; filename*=UTF-8''{name}"})
