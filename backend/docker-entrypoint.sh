#!/bin/sh
# Запуск контейнера приложения:
#   1) применяем миграции (база всегда в актуальной схеме);
#   2) запускаем веб-сервер.
set -e
mkdir -p "$UPLOAD_DIR"
alembic upgrade head
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 ${UVICORN_EXTRA:-}
