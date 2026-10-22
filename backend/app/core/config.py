"""Настройки приложения.

Все параметры читаются из переменных окружения (или файла .env).
Так один и тот же код работает на ноутбуке, в CI и на сервере компании —
меняются только переменные, а не код.
"""
from enum import StrEnum
from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class MonitoringMode(StrEnum):
    """Режим мониторинга узлов доступа.

    off      — опрос выключен;
    simulate — статусы имитируются (ноутбук: нет сетевого доступа к узлам);
    ping     — реальный ICMP-опрос (сервер компании).
    """

    off = "off"
    simulate = "simulate"
    ping = "ping"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ГИС ВОЛС"
    environment: str = "dev"

    # Подключение к PostgreSQL + PostGIS. Драйвер psycopg 3.
    database_url: str = "postgresql+psycopg://vols:vols@localhost:5432/vols"

    # Секрет для подписи токенов авторизации. В продакшене обязательно заменить.
    secret_key: str = "dev-only-secret-change-me-on-server-0123456789"  # ≥ 32 байт для HS256
    access_token_minutes: int = 12 * 60

    # Каталог для загруженных файлов (фото, документы, пакеты ИД)
    upload_dir: Path = Path("/data/uploads")

    # Каталог статических файлов фронтенда
    frontend_dir: Path = Path(__file__).resolve().parents[3] / "frontend"

    # Мониторинг узлов доступа
    monitoring_mode: MonitoringMode = MonitoringMode.simulate
    # Часовой пояс, в котором показываются даты (сервер может работать в UTC)
    display_tz: str = "Europe/Moscow"
    monitoring_interval_min: int = 5
    monitoring_fail_threshold: int = 3

    # Допуск привязки концов линий к узлам при импорте, метры
    snap_tolerance_m: float = 10.0

    # Вход с телефона по HTTPS (этап 10).
    # Порт HTTPS-прокси Caddy (deploy/Caddyfile)
    https_port: int = 8443
    # Адрес ноутбука/сервера в локальной сети, на который выпущен HTTPS-сертификат (пишет phone.bat)
    phone_host: str = ""

    # Язык Tesseract для OCR
    tesseract_lang: str = "rus"


@lru_cache
def get_settings() -> Settings:
    """Единственный экземпляр настроек на процесс."""
    return Settings()
