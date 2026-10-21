"""Проверка работоспособности: приложение запущено, база и PostGIS доступны."""
from fastapi import APIRouter, Depends
from sqlalchemy import text
from sqlalchemy.orm import Session

from app import __version__
from app.core.config import get_settings
from app.core.db import get_db

router = APIRouter(tags=["Служебное"])


@router.get("/health", summary="Состояние системы")
def health(db: Session = Depends(get_db)) -> dict:
    s = get_settings()
    result = {"status": "ok", "version": __version__, "environment": s.environment,
              "monitoring_mode": s.monitoring_mode.value}
    try:
        result["database"] = db.execute(text("select version()")).scalar_one().split(",")[0]
        result["postgis"] = db.execute(text("select postgis_lib_version()")).scalar_one()
    except Exception as exc:  # база недоступна или нет PostGIS — сообщаем, а не падаем
        result.update(status="degraded", error=str(exc).splitlines()[0])
    return result
