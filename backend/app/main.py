"""Точка входа веб-приложения.

Одно приложение отдаёт и REST API (/api/...), и статический фронтенд (/).
Документация API генерируется автоматически: /api/docs (Swagger UI).
"""
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import (
    asbuilt,
    auth,
    crs,
    drawing,
    edit,
    experiments,
    health,
    imports,
    incidents,
    locate,
    map,
    monitoring,
    overlays,
    phone,
    photos,
    splicing,
    stats,
)
from app.core.auth import current_user
from app.core.config import get_settings
from app.core.db import SessionLocal
from app.services.monitoring import Scheduler as MonitorScheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    # Фоновый опрос узлов доступа (режим и интервал — из настроек)
    scheduler = MonitorScheduler(SessionLocal)
    scheduler.start()
    yield
    scheduler.stop()


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        lifespan=lifespan,
        title=s.app_name,
        version=__version__,
        description="ГИС учёта волоконно-оптических линий связи с функцией "
                    "автоматизированной локализации повреждений.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.include_router(health.router, prefix="/api")          # открыт: проверка работоспособности
    app.include_router(phone.router, prefix="/api")           # открыт: нужен Caddy и телефону до входа
    app.include_router(auth.router, prefix="/api")
    app.include_router(stats.router, prefix="/api", dependencies=[Depends(current_user)])
    app.include_router(map.router, prefix="/api")
    app.include_router(imports.router, prefix="/api")
    app.include_router(edit.router, prefix="/api")
    app.include_router(drawing.router, prefix="/api")
    app.include_router(splicing.router, prefix="/api")
    app.include_router(locate.router, prefix="/api")
    app.include_router(incidents.router, prefix="/api")
    app.include_router(asbuilt.router, prefix="/api")
    app.include_router(monitoring.router, prefix="/api")
    app.include_router(photos.router, prefix="/api")
    app.include_router(experiments.router, prefix="/api")
    app.include_router(crs.router, prefix="/api")
    app.include_router(overlays.router, prefix="/api")

    # Статику подключаем последней: маршрут "/" не должен перекрывать /api
    if s.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=s.frontend_dir, html=True), name="frontend")
    return app


app = create_app()
