"""Точка входа веб-приложения.

Одно приложение отдаёт и REST API (/api/...), и статический фронтенд (/).
Документация API генерируется автоматически: /api/docs (Swagger UI).
"""
from fastapi import Depends, FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import auth, drawing, edit, health, imports, map, stats
from app.core.auth import current_user
from app.core.config import get_settings


def create_app() -> FastAPI:
    s = get_settings()
    app = FastAPI(
        title=s.app_name,
        version=__version__,
        description="ГИС учёта волоконно-оптических линий связи с функцией "
                    "автоматизированной локализации повреждений.",
        docs_url="/api/docs",
        redoc_url=None,
        openapi_url="/api/openapi.json",
    )
    app.include_router(health.router, prefix="/api")          # открыт: проверка работоспособности
    app.include_router(auth.router, prefix="/api")
    app.include_router(stats.router, prefix="/api", dependencies=[Depends(current_user)])
    app.include_router(map.router, prefix="/api")
    app.include_router(imports.router, prefix="/api")
    app.include_router(edit.router, prefix="/api")
    app.include_router(drawing.router, prefix="/api")

    # Статику подключаем последней: маршрут "/" не должен перекрывать /api
    if s.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=s.frontend_dir, html=True), name="frontend")
    return app


app = create_app()
