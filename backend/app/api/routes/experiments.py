"""Эксперимент для ВКР: запуск серий опытов, ход расчёта, результаты, отчёт и таблицы."""
from typing import Literal

import sqlalchemy as sa
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.api.routes.edit import _get
from app.core.auth import require_role
from app.core.db import SessionLocal, get_db
from app.models import AppUser, ExperimentRun, Role
from app.services import evaluation
from app.services.editing import EditError

router = APIRouter(tags=["Эксперимент"])
admin_only = require_role(Role.admin)


class StartIn(BaseModel):
    suite: Literal["quick", "full"] = "quick"
    only: list[str] | None = None


@router.get("/experiments", summary="Прогоны эксперимента", dependencies=[Depends(admin_only)])
def list_runs(db: Session = Depends(get_db)) -> list[dict]:
    runs = db.scalars(sa.select(ExperimentRun).order_by(ExperimentRun.id.desc()).limit(30)).all()
    return [evaluation.run_summary(r) for r in runs]


@router.post("/experiments", summary="Запустить эксперимент (в отдельной базе, в фоне)")
def start(data: StartIn, user: AppUser = Depends(admin_only)) -> dict:
    try:
        run_id = evaluation.start(SessionLocal, data.suite, data.only, user.id)
    except EditError as exc:
        raise HTTPException(409, str(exc)) from None
    return {"id": run_id}


@router.get("/experiments/{run_id}", summary="Прогон: ход и результаты", dependencies=[Depends(admin_only)])
def get_run(run_id: int, db: Session = Depends(get_db)) -> dict:
    r = _get(db, ExperimentRun, run_id)
    return {**evaluation.run_summary(r), "results": r.results}


def _done(db: Session, run_id: int) -> ExperimentRun:
    r = _get(db, ExperimentRun, run_id)
    if r.status != "done":
        raise HTTPException(409, "Прогон ещё не закончен")
    return r


@router.get("/experiments/{run_id}/report.md", summary="Отчёт для текста ВКР (Markdown)",
            dependencies=[Depends(admin_only)])
def report(run_id: int, db: Session = Depends(get_db)) -> Response:
    r = _done(db, run_id)
    return Response(evaluation.to_markdown(r.results).encode("utf-8"), media_type="text/markdown; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="experiment_{run_id}.md"'})


@router.get("/experiments/{run_id}/{series}.csv", summary="Таблица серии в CSV (Excel)",
            dependencies=[Depends(admin_only)])
def series_csv(run_id: int, series: str, db: Session = Depends(get_db)) -> Response:
    r = _done(db, run_id)
    if series not in (r.results or {}).get("results", {}):
        raise HTTPException(404, "Такой серии в прогоне нет")
    return Response(evaluation.to_csv(r.results, series), media_type="text/csv; charset=utf-8",
                    headers={"Content-Disposition": f'attachment; filename="experiment_{run_id}_{series}.csv"'})


@router.delete("/experiments/{run_id}", summary="Удалить прогон")
def delete_run(run_id: int, db: Session = Depends(get_db), user: AppUser = Depends(admin_only)) -> dict:
    r = _get(db, ExperimentRun, run_id)
    if r.status == "running":
        raise HTTPException(409, "Прогон ещё идёт")
    db.delete(r)
    db.commit()
    return {"ok": True}
