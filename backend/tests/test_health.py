"""Каркас приложения: API отвечает, база и PostGIS подключены, фронтенд отдаётся."""


def test_health_reports_postgis(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok", body
    assert body["database"].startswith("PostgreSQL 16")
    assert body["postgis"].startswith("3.")


def test_openapi_docs_available(client):
    assert client.get("/api/docs").status_code == 200
    schema = client.get("/api/openapi.json").json()
    assert "/api/health" in schema["paths"]


def test_frontend_served(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "ГИС ВОЛС" in r.text
