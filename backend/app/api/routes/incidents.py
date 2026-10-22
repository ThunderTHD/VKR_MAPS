"""Журнал повреждений и калибровка."""
from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.api.routes.edit import _get, _run
from app.core.auth import current_user, require_role
from app.core.db import get_db
from app.models import AppUser, CalibrationRun, Incident, Role
from app.services import calibration, incidents

router = APIRouter(tags=["Повреждения и калибровка"])
admin_only = require_role(Role.admin)
installer_or_admin = require_role(Role.installer)


class ActualIn(BaseModel):
    lon: float = Field(ge=-180, le=180)
    lat: float = Field(ge=-90, le=90)
    restored_at: datetime | None = None
    note: str | None = None


class IncidentUpdate(BaseModel):
    title: str | None = None
    note: str | None = None
    status: str | None = Field(None, pattern="^(cancelled|localized)$")


class CalibrateIn(BaseModel):
    holdout: float = Field(0.3, ge=0, le=0.5)
    seed: int = 1
    simulate: bool = True


@router.get("/incidents", summary="Журнал повреждений", dependencies=[Depends(current_user)])
def list_incidents(status: str | None = None, db: Session = Depends(get_db)) -> dict:
    return {"items": incidents.list_incidents(db, status), "stats": incidents.stats(db)}


@router.post("/incidents/{inc_id}/actual", summary="Отметить фактическое место (повреждение устранено)")
def set_actual(inc_id: int, data: ActualIn, db: Session = Depends(get_db),
               user: AppUser = Depends(installer_or_admin)) -> dict:
    inc = _run(db, incidents.set_actual, db, _get(db, Incident, inc_id), data.lon, data.lat,
               restored_at=data.restored_at, note=data.note, user_id=user.id)
    return {"ok": True, "error_m": inc.error_m, "actual": inc.calc_details.get("actual")}


@router.patch("/incidents/{inc_id}", summary="Изменить или отменить повреждение")
def patch_incident(inc_id: int, data: IncidentUpdate, db: Session = Depends(get_db),
                   user: AppUser = Depends(installer_or_admin)) -> dict:
    _run(db, incidents.update_incident, db, _get(db, Incident, inc_id), data.model_dump(exclude_unset=True), user.id)
    return {"ok": True}


@router.delete("/incidents/{inc_id}", summary="Удалить повреждение")
def delete_incident(inc_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    _run(db, incidents.delete_incident, db, _get(db, Incident, inc_id), user.id)
    return {"ok": True}


@router.get("/calibration/runs", summary="Прогоны калибровки", dependencies=[Depends(admin_only)])
def runs(db: Session = Depends(get_db)) -> list[dict]:
    rows = db.query(CalibrationRun).order_by(CalibrationRun.id.desc()).limit(20).all()
    return [calibration.run_summary(r) for r in rows]


@router.post("/calibration/runs", summary="Рассчитать калибровку (без применения)")
def new_run(data: CalibrateIn, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    cr = _run(db, calibration.run, db, holdout=data.holdout, seed=data.seed, simulate=data.simulate, user_id=user.id)
    return calibration.run_summary(cr)


@router.post("/calibration/runs/{run_id}/apply", summary="Применить калибровку")
def apply_run(run_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    cr = _run(db, calibration.apply, db, _get(db, CalibrationRun, run_id), user.id)
    return calibration.run_summary(cr)


@router.post("/calibration/runs/{run_id}/revert", summary="Отменить применённую калибровку")
def revert_run(run_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    cr = _run(db, calibration.revert, db, _get(db, CalibrationRun, run_id), user.id)
    return calibration.run_summary(cr)
