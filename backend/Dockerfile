# Образ приложения: Python + системные утилиты для OCR и опроса узлов.
# Собирается из корня репозитория (нужны и backend/, и frontend/).
FROM python:3.12-slim

# tesseract-ocr-rus — распознавание сканов ИД и фото с координатами;
# fping — параллельный ICMP-опрос узлов доступа (используется на сервере компании);
# curl — проверка здоровья контейнера;
# fonts-dejavu-core — шрифт с кириллицей для демонстрационных файлов (python -m app.cli demo-files).
RUN apt-get update \
 && apt-get install -y --no-install-recommends tesseract-ocr tesseract-ocr-rus fping curl fonts-dejavu-core \
 && rm -rf /var/lib/apt/lists/*

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FRONTEND_DIR=/app/frontend \
    UPLOAD_DIR=/data/uploads

WORKDIR /app/backend

# Сначала зависимости (кэшируется слоем, пока не меняется pyproject.toml)
COPY backend/pyproject.toml ./
RUN mkdir app && echo '__version__ = "0"' > app/__init__.py \
 && pip install ".[dev]" && rm -rf app

COPY backend/ ./
COPY frontend/ /app/frontend/
RUN pip install --no-deps . && chmod +x docker-entrypoint.sh

EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=5s --start-period=30s CMD curl -fs http://localhost:8000/api/health || exit 1
ENTRYPOINT ["./docker-entrypoint.sh"]
