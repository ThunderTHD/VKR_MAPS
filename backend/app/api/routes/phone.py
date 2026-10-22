"""Вход с телефона по HTTPS: адрес для QR-кода на странице phone.html.

Геолокация и камера в браузере телефона работают только на защищённом (https) сайте.
Перед приложением стоит Caddy (deploy/Caddyfile), который выпускает сертификат своим
локальным центром сертификации на адрес ноутбука в Wi-Fi-сети (PHONE_HOST в .env).
Корневой сертификат для установки на телефон Caddy отдаёт сам (/api/phone/root.crt) —
закрытый ключ центра сертификации приложению не доступен.
"""
from fastapi import APIRouter, Request

from app.core.config import get_settings

router = APIRouter(tags=["Служебное"])


@router.get("/phone/info", summary="Как подключиться с телефона")
def phone_info(request: Request) -> dict:
    s = get_settings()
    url = f"https://{s.phone_host}:{s.https_port}/" if s.phone_host else None
    return {"url": url, "host": s.phone_host or None, "https_port": s.https_port,
            "secure": request.url.scheme == "https"}
