"""Точка входа веб-приложения.

Одно приложение отдаёт и REST API (/api/...), и статический фронтенд (/).
Документация API генерируется автоматически: /api/docs (Swagger UI).
"""
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from app import __version__
from app.api.routes import health
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
    app.include_router(health.router, prefix="/api")

    # Статику подключаем последней: маршрут "/" не должен перекрывать /api
    if s.frontend_dir.is_dir():
        app.mount("/", StaticFiles(directory=s.frontend_dir, html=True), name="frontend")
    return app


app = create_app()
