"""Этап 10: работа в поле — что рядом, фото к повреждению."""
import pytest
import sqlalchemy as sa

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import AppUser, Role
from app.services import documents, field, splicing
from app.services.generator import reset_network
from tests.test_calibration import _incident
from tests.test_localization import _build

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16


@pytest.fixture()
def net(db):
    return _build(db)


def test_nearby_sorted_by_distance(db, net):
    splicing.create_closure(db, node_id=net["n"]["B"].id, number="М-7")
    r = field.nearby(db, lon=50.0001, lat=55.0, radius_m=150)
    labels = [o["label"] for o in r["objects"]]
    assert labels[0] == "к.№A" and r["objects"][0]["distance_m"] <= 7
    assert "Муфта М-7" in labels and "к.№C" not in labels  # C в ≈300 м — за радиусом
    assert [o["distance_m"] for o in r["objects"]] == sorted(o["distance_m"] for o in r["objects"])
    b = next(o for o in r["objects"] if o["label"] == "к.№B")
    assert b["closures"] == 1 and b["kind_ru"] == "Колодец"


def test_nearby_incidents_in_work_only(db, net):
    inc = _incident(db, net, 150)
    r = field.nearby(db, lon=50.0, lat=55.0)
    assert [i["id"] for i in r["incidents"]] == [inc.id] and r["incidents"][0]["distance_m"] > 100
    from app.services import incidents
    incidents.update_incident(db, inc, {"status": "cancelled"}, None)
    assert field.nearby(db, lon=50.0, lat=55.0)["incidents"] == []


def test_photo_attached_to_incident(db, net):
    inc = _incident(db, net, 150)
    doc = documents.attach(db, owner_type="incident", owner_id=inc.id, filename="IMG_0001.jpg",
                           data=b"\xff\xd8\xff" + b"0" * 10, kind="photo")
    assert documents.list_documents(db, "incident", inc.id)[0]["inline"] is True
    assert documents.open_document(doc)[2] is True
    heic = documents.attach(db, owner_type="incident", owner_id=inc.id, filename="IMG_0002.HEIC", data=b"ftyp" * 4,
                            kind="photo")
    assert documents.open_document(heic)[2] is False  # HEIC браузеры не показывают — только скачать


PASSWORD = "Тест-пароль-10"
H = {"X-Requested-With": "fetch"}


@pytest.fixture()
def api(client):
    with SessionLocal() as s:
        ids = _build(s)
        inc = _incident(s, ids, 150)
        s.execute(sa.delete(AppUser).where(AppUser.login == "f_inst"))
        s.add(AppUser(login="f_inst", full_name="f_inst", role=Role.installer, password_hash=hash_password(PASSWORD)))
        s.commit()
        info = {"inc": inc.id}
    yield client, info
    with SessionLocal() as s:
        reset_network(s)
        s.execute(sa.text("TRUNCATE document RESTART IDENTITY CASCADE"))
        s.commit()


def test_api_nearby_and_photo(api):
    c, ids = api
    assert c.get("/api/nearby", params={"lon": 50.0, "lat": 55.0}).status_code == 401
    assert c.post("/api/auth/login", json={"login": "f_inst", "password": PASSWORD}).status_code == 200
    r = c.get("/api/nearby", params={"lon": 50.0, "lat": 55.0, "radius": 120})
    assert r.status_code == 200 and r.json()["objects"][0]["label"] == "к.№A" and r.json()["incidents"]
    assert c.get("/api/nearby", params={"lon": 500, "lat": 55}).status_code == 422
    form = {"owner_type": "incident", "owner_id": str(ids["inc"]), "kind": "photo"}
    r = c.post("/api/documents", headers=H, data=form, files={"file": ("место.png", PNG, "image/png")})
    assert r.status_code == 200
    docs = c.get("/api/documents", params={"owner_type": "incident", "owner_id": ids["inc"]}).json()
    assert docs[0]["kind"] == "photo"
