"""Этап 10: страница входа с телефона — HTTPS-адрес ноутбука/сервера."""
from app.core import config


def test_info_without_host(client):
    body = client.get("/api/phone/info").json()
    assert body["https_port"] == 8443 and body["url"] is None and body["secure"] is False


def test_info_with_host(client, monkeypatch):
    monkeypatch.setenv("PHONE_HOST", "192.168.1.15")
    config.get_settings.cache_clear()
    try:
        assert client.get("/api/phone/info").json()["url"] == "https://192.168.1.15:8443/"
    finally:
        config.get_settings.cache_clear()


def test_https_behind_proxy(client):
    # Caddy передаёт X-Forwarded-Proto; uvicorn с --proxy-headers превращает его в схему запроса.
    # В тестовом клиенте прокси нет — проверяем, что без заголовка запрос считается http.
    assert client.get("/api/phone/info").json()["secure"] is False
