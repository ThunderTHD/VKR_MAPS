"""Авторизация и API карты: доступ, слои GeoJSON, фильтры, поиск, карточки."""
import pytest
import sqlalchemy as sa

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import AppUser, Closure, Operator, Owner, Role
from app.services.generator import GeneratorParams, generate_network

PASSWORD = "Тест-пароль-1"


@pytest.fixture(scope="module")
def network():
    """Сеть и пользователи создаются один раз на модуль (генерация — несколько секунд)."""
    with SessionLocal() as db:
        generate_network(db, GeneratorParams(nodes=80, seed=11, buildings_per_operator=4))
        db.execute(sa.text("DELETE FROM audit_log"))
        db.execute(sa.delete(AppUser))
        for login, role in (("t_admin", Role.admin), ("t_viewer", Role.viewer)):
            db.add(AppUser(login=login, full_name=login, role=role, password_hash=hash_password(PASSWORD)))
        db.commit()
    yield


@pytest.fixture()
def logged(client, network):
    r = client.post("/api/auth/login", json={"login": "t_viewer", "password": PASSWORD})
    assert r.status_code == 200
    return client


def test_api_requires_login(client, network):
    assert client.get("/api/map/layers/nodes").status_code == 401
    assert client.get("/api/stats").status_code == 401
    assert client.get("/api/health").status_code == 200  # проверка работоспособности открыта


def test_wrong_password_rejected(client, network):
    r = client.post("/api/auth/login", json={"login": "t_viewer", "password": "не тот"})
    assert r.status_code == 401
    assert r.json()["detail"] == "Неверный логин или пароль"
    r = client.post("/api/auth/login", json={"login": "нет_такого", "password": PASSWORD})
    assert r.json()["detail"] == "Неверный логин или пароль"  # одинаковое сообщение — не раскрываем логины


def test_login_sets_httponly_cookie(client, network):
    r = client.post("/api/auth/login", json={"login": "t_admin", "password": PASSWORD})
    cookie = r.headers["set-cookie"]
    assert "vols_session=" in cookie and "HttpOnly" in cookie and "SameSite=lax" in cookie
    assert client.get("/api/auth/me").json()["role"] == "admin"
    client.post("/api/auth/logout", headers={"X-Requested-With": "fetch"})
    assert client.get("/api/auth/me").status_code == 401


def test_forged_token_rejected(client, network):
    client.cookies.set("vols_session", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxIn0.forged-signature")
    assert client.get("/api/auth/me").status_code == 401


@pytest.mark.parametrize("layer", ["segments", "nodes", "cables", "closures", "access_nodes", "jumpers",
                                   "unclassified"])
def test_layers_are_geojson(logged, layer):
    r = logged.get(f"/api/map/layers/{layer}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/geo+json")
    fc = r.json()
    assert fc["type"] == "FeatureCollection"
    for f in fc["features"]:
        assert f["type"] == "Feature" and f["geometry"]["type"] and "id" in f["properties"]


def test_operator_filter(logged):
    with SessionLocal() as db:
        op = db.scalar(sa.select(Operator).order_by(Operator.id))
        own_closures = db.scalar(sa.select(sa.func.count(Closure.id)).where(Closure.operator_id == op.id))
    all_cables = logged.get("/api/map/layers/cables").json()["features"]
    cables = logged.get("/api/map/layers/cables", params={"operator_id": op.id}).json()["features"]
    assert 0 < len(cables) < len(all_cables)
    assert all(f["properties"]["operator_id"] == op.id for f in cables)
    closures = logged.get("/api/map/layers/closures", params={"operator_id": op.id}).json()["features"]
    assert len(closures) == own_closures
    segs = logged.get("/api/map/layers/segments", params={"operator_id": op.id}).json()["features"]
    assert segs and all(op.id in f["properties"]["operators"] for f in segs)


def test_owner_filter(logged):
    with SessionLocal() as db:
        owner = db.scalar(sa.select(Owner).where(Owner.short_name == "Таттелеком"))
    segs = logged.get("/api/map/layers/segments", params={"owner_id": owner.id}).json()["features"]
    assert segs and all(f["properties"]["owner_id"] == owner.id for f in segs)


def test_bbox_filter(logged):
    ref = logged.get("/api/map/reference").json()
    x1, y1, x2, y2 = ref["extent"]
    half = f"{x1},{y1},{(x1 + x2) / 2},{(y1 + y2) / 2}"
    all_nodes = logged.get("/api/map/layers/nodes").json()["features"]
    part = logged.get("/api/map/layers/nodes", params={"bbox": half}).json()["features"]
    assert 0 < len(part) < len(all_nodes)
    assert logged.get("/api/map/layers/nodes", params={"bbox": "1,2,3"}).status_code == 422


def test_stats_after_login(logged):
    body = logged.get("/api/stats").json()
    assert body["Узлы сети"] > 0 and "Волокна" in body


def test_reference(logged):
    ref = logged.get("/api/map/reference").json()
    assert {o["name"] for o in ref["operators"]} == {"Ростелеком", "Билайн", "МегаФон", "МТС"}
    assert ref["length_sources"][0]["code"] == "reflectometer"
    assert len(ref["extent"]) == 4


def test_search_normalizes_well_codes(logged):
    with SessionLocal() as db:
        code = db.execute(sa.text("SELECT code FROM network_node WHERE kind = 'well' LIMIT 1")).scalar_one()
    digits = code.replace("к.№", "")
    for query in (code, f"к{digits}", f"к.{digits}", f"к. № {digits}"):
        results = logged.get("/api/search", params={"q": query}).json()
        assert any(r["label"].startswith(code) for r in results), query


def test_search_by_ip_and_closure(logged):
    assert logged.get("/api/search", params={"q": "10.100."}).json()[0]["type"] == "access_node"
    assert any(r["type"] == "closure" for r in logged.get("/api/search", params={"q": "М1-1"}).json())
    assert logged.get("/api/search", params={"q": "я"}).json() == []  # слишком короткий запрос


@pytest.mark.parametrize("obj_type", ["node", "segment", "cable", "closure", "cross", "access_node"])
def test_cards(logged, obj_type):
    table = {"node": "network_node", "segment": "route_segment", "cable": "cable", "closure": "closure",
             "cross": "optical_cross", "access_node": "access_node"}[obj_type]
    with SessionLocal() as db:
        obj_id = db.execute(sa.text(f"SELECT min(id) FROM {table}")).scalar_one()
    card = logged.get(f"/api/objects/{obj_type}/{obj_id}").json()
    assert card["type"] == obj_type and card["title"] and card["fields"]
    for section in card["sections"]:
        for item in section["items"]:
            assert {"type", "id", "label"} <= item.keys()


def test_closure_card_has_splice_table(logged):
    with SessionLocal() as db:
        cl = db.scalar(sa.select(Closure).order_by(Closure.id))
    card = logged.get(f"/api/objects/closure/{cl.id}").json()
    assert card["table"]["columns"][0] == "Кабель A" and card["table"]["rows"]
    assert any(s["title"] == "Кабели в муфте" and s["items"] for s in card["sections"])


def test_card_not_found(logged):
    assert logged.get("/api/objects/node/999999").status_code == 404
    assert logged.get("/api/objects/nonsense/1").status_code == 422


def test_unsafe_method_requires_header(network):
    """Изменяющие запросы без X-Requested-With отклоняются (защита от CSRF)."""
    from fastapi import Depends, FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import auth
    from app.core.auth import current_user

    probe = FastAPI()
    probe.include_router(auth.router, prefix="/api")

    @probe.post("/api/probe")
    def _probe(user=Depends(current_user)):
        return {"ok": True}

    with TestClient(probe) as c:
        c.post("/api/auth/login", json={"login": "t_admin", "password": PASSWORD})
        assert c.post("/api/probe").status_code == 403
        assert c.post("/api/probe", headers={"X-Requested-With": "fetch"}).status_code == 200
