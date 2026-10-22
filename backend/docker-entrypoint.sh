#!/bin/sh
# Запуск контейнера приложения:
#   1) применяем миграции (база всегда в актуальной схеме);
#   2) запускаем веб-сервер.
set -e
mkdir -p "$UPLOAD_DIR"
alembic upgrade head
# --proxy-headers: за HTTPS-прокси (Caddy) приложение видит, что запрос пришёл по https,
# и ставит cookie входа с флагом Secure
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips "${FORWARDED_ALLOW_IPS:-*}" ${UVICORN_EXTRA:-}
