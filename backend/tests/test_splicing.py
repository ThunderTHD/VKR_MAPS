"""Этап 6: муфты, рассветка, разрезание кабеля, кроссы, документы."""
import pytest
import sqlalchemy as sa

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import (
    AppUser,
    CableReserve,
    CableType,
    CrossPort,
    Document,
    FiberEnd,
    NetworkNode,
    NodeKind,
    Operator,
    Role,
    RouteSegment,
    SegmentKind,
    Splice,
)
from app.services import documents, routing, splicing
from app.services.editing import EditError
from app.services.generator import reset_network
from app.services.network_ops import refresh_segment_lengths


def _build(db):
    """A—B—C по прямой и ответвление B—D. Кабель X: A→C транзитом через B; кабель Y: B→D. Муфта в B."""
    db.execute(sa.text("TRUNCATE network_node, route_segment, closure, cable, optical_cross, audit_log, document "
                       "RESTART IDENTITY CASCADE"))
    pts = {"A": (50.0, 55.0), "B": (50.0016, 55.0), "C": (50.0032, 55.0), "D": (50.0016, 55.001)}
    n = {}
    for k, (x, y) in pts.items():
        n[k] = NetworkNode(kind=NodeKind.well, code=f"к.№{k}", geom=f"SRID=4326;POINT({x} {y})")
        db.add(n[k])
    db.flush()
    for a, b in (("A", "B"), ("B", "C"), ("B", "D")):
        (x1, y1), (x2, y2) = pts[a], pts[b]
        db.add(RouteSegment(kind=SegmentKind.duct, node_a_id=n[a].id, node_b_id=n[b].id, geom_length_m=0,
                            geom=f"SRID=4326;LINESTRING({x1} {y1}, {x2} {y2})"))
    db.flush()
    refresh_segment_lengths(db)
    op = db.scalar(sa.select(Operator.id).order_by(Operator.id))
    ctype = db.scalar(sa.select(CableType).where(CableType.modules * CableType.fibers_per_module == 8))
    x = routing.create_cable(db, name="X", operator_id=op, cable_type_id=ctype.id, waypoints=[n["A"].id, n["C"].id],
                             reserves=[{"node_id": n["C"].id, "length_m": 10}])
    y = routing.create_cable(db, name="Y", operator_id=op, cable_type_id=ctype.id, waypoints=[n["B"].id, n["D"].id])
    clo = splicing.create_closure(db, node_id=n["B"].id, number="М-1", operator_id=op)
    return {"n": n, "x": x, "y": y, "clo": clo, "op": op, "fibers": ctype.fiber_count}


@pytest.fixture()
def net(db):
    return _build(db)


def test_sheet_shows_ends_and_transit(db, net):
    sheet = splicing.closure_sheet(db, net["clo"])
    assert [(c["name"], c["end"]) for c in sheet["cables"]] == [("Y", "a")]
    assert [t["name"] for t in sheet["transit"]] == ["X"]
    assert any("без разреза" in i["message"] for i in sheet["issues"])
    assert all(f["state"] == "free" for f in sheet["cables"][0]["fibers"])
    assert sheet["cables"][0]["fibers"][0]["hex"].startswith("#")


def test_cannot_splice_transit_cable(db, net):
    with pytest.raises(EditError, match="разрежьте"):
        splicing.add_splice(db, net["clo"], net["x"].fibers[0].id, net["y"].fibers[0].id)


def test_split_cable_keeps_optics(db, net):
    x, clo, n = net["x"], net["clo"], net["n"]
    # До разреза: конец b кабеля X заведён на кросс в C
    cross = splicing.create_cross(db, node_id=n["C"].id, name="ODF-1", port_count=8)
    splicing.connect_ports(db, cross, cable_id=x.id, fiber_from=1, port_from=1, count=2)
    new = splicing.split_cable(db, x, clo, new_name="X-2")
    db.expire_all()
    x, new = db.get(type(x), x.id), db.get(type(new), new.id)
    assert x.end_node_id == n["B"].id and new.start_node_id == n["B"].id and new.end_node_id == n["C"].id
    assert splicing.cable_route_ok(db, x.id) and splicing.cable_route_ok(db, new.id)
    # Все волокна соединены транзитом, порты кросса и запас в C перешли к новому кабелю
    assert db.scalar(sa.select(sa.func.count(Splice.id)).where(Splice.closure_id == clo.id)) == net["fibers"]
    port_fibers = db.scalars(sa.select(CrossPort.fiber_id).where(CrossPort.cross_id == cross.id,
                                                                 CrossPort.fiber_id.is_not(None))).all()
    assert set(port_fibers) == {new.fibers[0].id, new.fibers[1].id}
    assert db.scalar(sa.select(CableReserve.cable_id).where(CableReserve.node_id == n["C"].id)) == new.id
    sheet = splicing.closure_sheet(db, clo)
    assert {(c["name"], c["end"]) for c in sheet["cables"]} == {("X", "b"), ("X-2", "a"), ("Y", "a")}
    assert sheet["transit"] == []
    # Геометрия обоих кусков не пустая и стыкуется в B
    touch = db.execute(sa.text("SELECT ST_Equals(ST_EndPoint(a.geom), ST_StartPoint(b.geom)) FROM cable a, cable b "
                               "WHERE a.id = :a AND b.id = :b"), {"a": x.id, "b": new.id}).scalar_one()
    assert touch


def test_branch_instead_of_transit(db, net):
    clo = net["clo"]
    new = splicing.split_cable(db, net["x"], clo)
    db.expire_all()
    x, y = db.get(type(net["x"]), net["x"].id), db.get(type(net["y"]), net["y"].id)
    transit = db.scalar(sa.select(Splice).where(Splice.fiber_a_id == x.fibers[0].id))
    splicing.delete_splice(db, transit, user_id=None)
    sp = splicing.add_splice(db, clo, x.fibers[0].id, y.fibers[0].id, loss_db=0.35, note="ответвление")
    assert sp.end_a == FiberEnd.b and sp.end_b == FiberEnd.a
    # Конец волокна занят — повторная сварка запрещена базой, сообщение понятное
    with pytest.raises(EditError, match="уже подключён"):
        splicing.add_splice(db, clo, x.fibers[0].id, y.fibers[1].id)
    sheet = splicing.closure_sheet(db, clo)
    assert any(i["level"] == "warning" and "0.35" in i["message"] for i in sheet["issues"])
    fiber = next(f for c in sheet["cables"] if c["name"] == "Y" for f in c["fibers"] if f["number"] == 1)
    assert fiber["state"] == "splice" and fiber["partner"] == {"cable_id": x.id, "cable": "X", "fiber": 1}
    assert new.id


def test_splice_range_all_or_nothing(db, net):
    clo = net["clo"]
    splicing.split_cable(db, net["x"], clo, transit=False)
    db.expire_all()
    x, y = db.get(type(net["x"]), net["x"].id), db.get(type(net["y"]), net["y"].id)
    splicing.add_splice(db, clo, x.fibers[2].id, y.fibers[7].id)  # занято волокно 3 кабеля X
    before = db.scalar(sa.select(sa.func.count(Splice.id)))
    with pytest.raises(EditError, match="Ничего не сварено"):
        splicing.splice_range(db, clo, cable_a_id=x.id, from_a=1, cable_b_id=y.id, from_b=1, count=4)
    assert db.scalar(sa.select(sa.func.count(Splice.id))) == before
    assert splicing.splice_range(db, clo, cable_a_id=x.id, from_a=4, cable_b_id=y.id, from_b=1, count=4) == 4
    with pytest.raises(EditError, match="нет волокон"):
        splicing.splice_range(db, clo, cable_a_id=x.id, from_a=8, cable_b_id=y.id, from_b=5, count=3)


def test_cross_ports(db, net):
    y, n = net["y"], net["n"]
    cross = splicing.create_cross(db, node_id=n["D"].id, name="ODF-1", port_count=8, connector="SC/UPC")
    assert splicing.connect_ports(db, cross, cable_id=y.id, fiber_from=1, port_from=1, count=4) == 4
    sheet = splicing.cross_sheet(db, cross)
    assert sheet["ports"][0]["fiber"]["end"] == "b" and sheet["ports"][4]["fiber"] is None
    with pytest.raises(EditError, match="Ничего не подключено"):
        splicing.connect_ports(db, cross, cable_id=y.id, fiber_from=5, port_from=4, count=2)  # порт 4 занят
    with pytest.raises(EditError, match="Ничего не подключено"):
        splicing.connect_ports(db, cross, cable_id=y.id, fiber_from=1, port_from=5, count=1)  # волокно 1 уже на порту
    with pytest.raises(EditError, match="заняты"):
        splicing.update_cross(db, cross, {"port_count": 2}, user_id=None)
    assert splicing.disconnect_ports(db, cross, 3, 2, user_id=None) == 2
    splicing.update_cross(db, cross, {"port_count": 2}, user_id=None)
    splicing.update_cross(db, cross, {"port_count": 16}, user_id=None)
    assert [p.port_no for p in cross.ports] == list(range(1, 17))


def test_delete_closure_removes_splices(db, net):
    clo = net["clo"]
    splicing.split_cable(db, net["x"], clo)
    assert splicing.delete_closure(db, clo, user_id=None) == net["fibers"]
    assert db.scalar(sa.select(sa.func.count(Splice.id))) == 0


# ------------------------------------------------------------------ документы

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32


def test_documents_attach_and_inline_rules(db, net, tmp_path):
    clo = net["clo"]
    doc = documents.attach(db, owner_type="closure", owner_id=clo.id, filename="../../схема.png", data=PNG,
                           kind="splice_scheme", note="фото схемы")
    assert ".." not in doc.path and doc.mime == "image/png"
    assert documents.open_document(doc)[2] is True  # настоящая картинка — показываем в браузере
    fake = documents.attach(db, owner_type="closure", owner_id=clo.id, filename="evil.png",
                            data=b"<html><script>alert(1)</script>")
    data, mime, inline = documents.open_document(fake)
    assert inline is False and mime == "application/octet-stream"
    with pytest.raises(EditError, match="не поддерживается"):
        documents.attach(db, owner_type="closure", owner_id=clo.id, filename="page.html", data=b"<html>")
    with pytest.raises(EditError, match="не найден"):
        documents.attach(db, owner_type="closure", owner_id=999999, filename="a.pdf", data=b"%PDF-1.4")
    listed = documents.list_documents(db, "closure", clo.id)
    assert {d["filename"] for d in listed} == {"../../схема.png", "evil.png"}
    from app.core.config import get_settings
    path = get_settings().upload_dir / doc.path
    assert path.exists()
    documents.delete_document(db, doc, user_id=None)
    assert not path.exists() and db.get(Document, doc.id) is None


def test_closure_csv(db, net):
    clo = net["clo"]
    splicing.split_cable(db, net["x"], clo)
    data = documents.closure_csv(splicing.closure_sheet(db, clo))
    text = data.decode("utf-8")
    assert text.startswith("﻿Муфта М-1;") and "Кабель A;Модуль;ОВ" in text
    assert text.count("\r\n") == 3 + net["fibers"]


# ------------------------------------------------------------------ API и права

PASSWORD = "Тест-пароль-6"
H = {"X-Requested-With": "fetch"}


@pytest.fixture()
def api_net(client):
    with SessionLocal() as s:
        ids = _build(s)
        s.execute(sa.delete(AppUser).where(AppUser.login.in_(["s_admin", "s_inst", "s_view"])))
        for login, role in (("s_admin", Role.admin), ("s_inst", Role.installer), ("s_view", Role.viewer)):
            s.add(AppUser(login=login, full_name=login, role=role, password_hash=hash_password(PASSWORD)))
        s.commit()
        info = {"clo": ids["clo"].id, "x": ids["x"].id, "y": ids["y"].id, "B": ids["n"]["B"].id,
                "D": ids["n"]["D"].id}
    yield client, info
    with SessionLocal() as s:
        reset_network(s)
        s.execute(sa.text("TRUNCATE document RESTART IDENTITY CASCADE"))
        s.commit()


def _login(client, who):
    assert client.post("/api/auth/login", json={"login": who, "password": PASSWORD}).status_code == 200


def test_api_rights_and_flow(api_net):
    c, ids = api_net
    _login(c, "s_view")
    assert c.get(f"/api/closures/{ids['clo']}/sheet").status_code == 200
    assert c.post(f"/api/closures/{ids['clo']}/split-cable", headers=H, json={"cable_id": ids["x"]}).status_code == 403
    _login(c, "s_inst")
    assert c.post(f"/api/closures/{ids['clo']}/split-cable", headers=H, json={"cable_id": ids["x"]}).status_code == 403
    _login(c, "s_admin")
    r = c.post(f"/api/closures/{ids['clo']}/split-cable", headers=H, json={"cable_id": ids["x"], "transit": False})
    assert r.status_code == 200, r.text
    _login(c, "s_inst")
    r = c.post(f"/api/closures/{ids['clo']}/splices/range", headers=H,
               json={"cable_a_id": ids["x"], "from_a": 1, "cable_b_id": ids["y"], "from_b": 1, "count": 2})
    assert r.status_code == 200, r.text
    sheet = c.get(f"/api/closures/{ids['clo']}/sheet").json()
    assert len(sheet["splices"]) == 2
    sid = sheet["splices"][0]["id"]
    assert c.patch(f"/api/splices/{sid}", headers=H, json={"loss_splicer_db": 0.04}).status_code == 200
    # Повторная сварка — 409 с текстом из базы
    r = c.post(f"/api/closures/{ids['clo']}/splices", headers=H,
               json={"fiber_a_id": sheet["splices"][0]["a"]["fiber_id"],
                     "fiber_b_id": sheet["splices"][1]["b"]["fiber_id"]})
    assert r.status_code == 409 and "подключён" in r.json()["detail"]
    csv = c.get(f"/api/closures/{ids['clo']}/sheet.csv")
    assert csv.status_code == 200 and "attachment" in csv.headers["content-disposition"]
    # Документ: монтажник прикрепляет фото, удалить может только администратор
    r = c.post("/api/documents", headers=H, data={"owner_type": "closure", "owner_id": str(ids["clo"]),
                                                  "kind": "photo"}, files={"file": ("муфта.png", PNG, "image/png")})
    assert r.status_code == 200, r.text
    doc_id = r.json()["id"]
    f = c.get(f"/api/documents/{doc_id}/file")
    assert f.status_code == 200 and f.headers["content-type"] == "image/png" and f.content == PNG
    assert "inline" in f.headers["content-disposition"] and f.headers["x-content-type-options"] == "nosniff"
    assert c.delete(f"/api/documents/{doc_id}", headers=H).status_code == 403
    _login(c, "s_admin")
    assert c.delete(f"/api/documents/{doc_id}", headers=H).status_code == 200
    r = c.post("/api/crosses", headers=H, json={"node_id": ids["D"], "name": "ODF-2", "port_count": 8})
    assert r.status_code == 200
    r = c.post(f"/api/crosses/{r.json()['id']}/connect", headers=H,
               json={"cable_id": ids["y"], "fiber_from": 3, "port_from": 1, "count": 2})
    assert r.status_code == 200, r.text
