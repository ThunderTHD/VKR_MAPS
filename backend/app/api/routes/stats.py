"""Сводка по базе: сколько каких объектов."""
from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.db import get_db
from app.services.stats import collect_stats

router = APIRouter(tags=["Служебное"])


@router.get("/stats", summary="Сколько объектов в базе")
def stats(db: Session = Depends(get_db)) -> dict[str, float]:
    return {k: float(v) for k, v in collect_stats(db).items()}
