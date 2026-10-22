"""Этап 7: трассировка волокна по сваркам и локализация повреждения по рефлектометру."""
import math

import pytest
import sqlalchemy as sa

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import (
    AppUser,
    CableType,
    Incident,
    LengthSourceCode,
    NetworkNode,
    NodeKind,
    Operator,
    Role,
    RouteSegment,
    SegmentKind,
)
from app.services import experiment, localization, routing, splicing, tracing
from app.services.editing import EditError
from app.services.generator import GeneratorParams, generate_network, reset_network
from app.services.network_ops import refresh_segment_lengths

TW = 0.01


def _build(db):
    """A —100 м— B —200 м— C (длины по рефлектометру ±1 %), кабель X: A→C, укрутка 1 %, запас 20 м в B.

    Кроссы в A и C, волокно 1 заведено на порт 1 с обеих сторон. Кабель Y из B в D никуда не подключён.
    """
    db.execute(sa.text("TRUNCATE network_node, route_segment, closure, cable, optical_cross, incident, audit_log "
                       "RESTART IDENTITY CASCADE"))
    pts = {"A": (50.0, 55.0), "B": (50.0016, 55.0), "C": (50.0048, 55.0), "D": (50.0016, 55.001)}
    n = {}
    for k, (x, y) in pts.items():
        n[k] = NetworkNode(kind=NodeKind.well, code=f"к.№{k}", geom=f"SRID=4326;POINT({x} {y})")
        db.add(n[k])
    db.flush()
    segs = {}
    for a, b, fact in (("A", "B", 100.0), ("C", "B", 200.0), ("B", "D", None)):  # C—B нарисован «наоборот»
        (x1, y1), (x2, y2) = pts[a], pts[b]
        segs[a + b] = RouteSegment(kind=SegmentKind.duct, node_a_id=n[a].id, node_b_id=n[b].id, geom_length_m=0,
                                   fact_length_m=fact, length_source=LengthSourceCode.reflectometer if fact else None,
                                   geom=f"SRID=4326;LINESTRING({x1} {y1}, {x2} {y2})")
        db.add(segs[a + b])
    db.flush()
    refresh_segment_lengths(db)
    op = db.scalar(sa.select(Operator.id).order_by(Operator.id))
    ctype = db.scalar(sa.select(CableType).where(CableType.modules * CableType.fibers_per_module == 8))
    x = routing.create_cable(db, name="X", operator_id=op, cable_type_id=ctype.id, waypoints=[n["A"].id, n["C"].id],
                             twist_factor=TW, reserves=[{"node_id": n["B"].id, "length_m": 20, "source": "as_built"}])
    y = routing.create_cable(db, name="Y", operator_id=op, cable_type_id=ctype.id, waypoints=[n["B"].id, n["D"].id])
    xa = splicing.create_cross(db, node_id=n["A"].id, name="ODF-A", port_count=8)
    xc = splicing.create_cross(db, node_id=n["C"].id, name="ODF-C", port_count=8)
    splicing.connect_ports(db, xa, cable_id=x.id, fiber_from=1, port_from=1, count=1)
    splicing.connect_ports(db, xc, cable_id=x.id, fiber_from=1, port_from=1, count=1)
    db.flush()
    return {"n": n, "x": x, "y": y, "port_a": xa.ports[0].id, "port_c": xc.ports[0].id, "segs": segs}


@pytest.fixture()
def net(db):
    return _build(db)


def _trace(db, port_id):
    fiber, end, port = tracing.start_of_port(db, port_id)
    return tracing.trace(db, fiber.id, end, start_port=port)


def test_trace_profile(db, net):
    tr = _trace(db, net["port_a"])
    assert tr.end_state == "port" and len(tr.legs) == 1
    assert [e.kind for e in tr.elements] == ["segment", "reserve", "segment"]
    assert tr.total_phys == pytest.approx(320.0)
    assert tr.total_opt == pytest.approx(320.0 * (1 + TW))
    # Участок C—B нарисован от C к B, но свет идёт от B к C — геометрия развёрнута по ходу света
    last = tr.elements[-1]
    assert last.coords[0] == pytest.approx([50.0016, 55.0]) and last.coords[-1] == pytest.approx([50.0048, 55.0])
    assert [ev.kind for ev in tr.events] == ["start", "port"]
    assert "ODF-C" in tr.events[-1].label


def test_trace_from_other_end_is_mirror(db, net):
    tr = _trace(db, net["port_c"])
    assert [e.kind for e in tr.elements] == ["segment", "reserve", "segment"]
    assert tr.elements[0].length_m == 200.0 and tr.total_phys == pytest.approx(320.0)


def test_locate_inside_segment(db, net):
    tr = _trace(db, net["port_a"])
    r = localization.localize(tr, 50.0 * (1 + TW))
    assert r["position_m"] == pytest.approx(50.0, abs=0.01)
    assert r["place"]["kind"] == "segment" and r["place"]["segment_id"] == net["segs"]["AB"].id
    assert r["point"] == pytest.approx([50.0008, 55.0], abs=1e-7)   # середина нарисованного участка A—B
    assert "50 м от к.№A" in r["place"]["text"]
    assert 1.0 < r["half_width_m"] < 5.0
    assert sum(c["share_pct"] for c in r["contributions"]) == pytest.approx(100, abs=1)


def test_locate_in_reserve_and_corrections(db, net):
    tr = _trace(db, net["port_a"])
    # 100 м участка + 10 м запаса, прибор с шнуром 25 м
    r = localization.localize(tr, 110.0 * (1 + TW) + 25, launch_m=25)
    assert r["place"]["kind"] == "reserve" and "запасе" in r["place"]["text"]
    assert r["point"] == pytest.approx([50.0016, 55.0])
    # В приборе стоял показатель 1.46 вместо 1.4683 — показание пересчитывается
    d = localization.correct_distance(1000, ior_set=1.46, ior_fiber=1.4683)
    assert d == pytest.approx(1000 * 1.46 / 1.4683)


def test_locate_beyond_end_warns(db, net):
    tr = _trace(db, net["port_a"])
    r = localization.localize(tr, 400)
    assert r["position_m"] == pytest.approx(320.0)
    assert any("больше длины" in w for w in r["warnings"])


def test_interval_grows_with_uncertain_lengths(db, net):
    tr = _trace(db, net["port_a"])
    exact = localization.localize(tr, 250)["half_width_m"]
    net["segs"]["AB"].fact_length_m, net["segs"]["AB"].length_source = None, None  # длина только по карте
    db.flush()
    rough = localization.localize(_trace(db, net["port_a"]), 250)
    assert rough["half_width_m"] > 3 * exact
    assert any("только по карте" in w for w in rough["warnings"])
    assert "по карте" in rough["contributions"][0]["source"]


def test_two_sided_is_more_precise(db, net):
    tr = _trace(db, net["port_a"])
    true_pos = 250.0
    d1 = true_pos * (1 + TW)
    d2 = (320.0 - true_pos) * (1 + TW)
    one = localization.localize(tr, d1)
    both = localization.localize(tr, d1, far_distance_m=d2)
    assert both["position_m"] == pytest.approx(true_pos, abs=0.5)
    assert both["half_width_m"] < one["half_width_m"]
    assert both["two_sided"]["disagreement_m"] < 1
    # Сильно несогласованные показания — предупреждение
    bad = localization.localize(tr, d1, far_distance_m=d2 + 60)
    assert any("расходятся" in w for w in bad["warnings"])


def test_trace_through_splices_after_cut(db, net):
    """Разрез в муфте B (транзит) не меняет оптику: место то же, путь проходит через сварку."""
    clo = splicing.create_closure(db, node_id=net["n"]["B"].id, number="М-B")
    splicing.split_cable(db, net["x"], clo)
    db.expire_all()
    tr = _trace(db, net["port_a"])
    assert len(tr.legs) == 2 and [ev.kind for ev in tr.events] == ["start", "splice", "port"]
    assert tr.total_phys == pytest.approx(320.0)  # запас в B остался у первой части
    assert tr.events[1].phys_m == pytest.approx(120.0)
    r = localization.localize(tr, 250 * (1 + TW))
    assert r["position_m"] == pytest.approx(250.0, abs=0.01)


def test_open_end_and_errors(db, net):
    y = net["y"]
    tr = tracing.trace(db, y.fibers[0].id, "a")
    assert tr.end_state == "open" and "никуда не подключён" in tr.events[-1].label
    with pytest.raises(EditError, match="свободен"):
        free_port = db.scalar(sa.text("SELECT id FROM cross_port WHERE fiber_id IS NULL LIMIT 1"))
        tracing.start_of_port(db, free_port)
    with pytest.raises(EditError, match="другого конца"):
        localization.localize(tr, 10, far_distance_m=10)


def test_wells_in_interval_sorted_by_distance(db, net):
    tr = _trace(db, net["port_a"])
    r = localization.localize(tr, 101.0)  # у колодца B
    assert r["wells"][0]["label"] == "к.№B" and abs(r["wells"][0]["offset_m"]) < 1


def test_point_helpers():
    line = [[50.0, 55.0], [50.001, 55.0], [50.001, 55.001]]
    mid = localization.point_at(line, 0.5)
    total = localization._cum(line)[-1]
    assert localization.point_at(line, 0) == line[0] and localization.point_at(line, 1) == line[-1]
    assert mid[0] == pytest.approx(50.001)  # половина длины приходится на второе (более длинное) звено
    assert localization._cum(localization.sub_line(line, 0.25, 0.75))[-1] == pytest.approx(total / 2, rel=1e-6)
    assert not math.isnan(mid[0])


def test_experiment_on_generated_network(db):
    generate_network(db, GeneratorParams(nodes=60, seed=5, buildings_per_operator=4, measured_share=1.0))
    res = experiment.run_experiment(db, n=60, seed=3)
    assert res["paths"] > 0 and res["trials"] == 60
    assert res["coverage_pct"] >= 85      # интервал не занижен
    assert res["error_median_m"] < res["half_width_mean_m"]


# ------------------------------------------------------------------ API

PASSWORD = "Тест-пароль-7"
H = {"X-Requested-With": "fetch"}


@pytest.fixture()
def api_net(client):
    with SessionLocal() as s:
        ids = _build(s)
        s.execute(sa.delete(AppUser).where(AppUser.login.in_(["l_inst", "l_view"])))
        for login, role in (("l_inst", Role.installer), ("l_view", Role.viewer)):
            s.add(AppUser(login=login, full_name=login, role=role, password_hash=hash_password(PASSWORD)))
        s.commit()
        info = {"port_a": ids["port_a"], "x": ids["x"].id}
    yield client, info
    with SessionLocal() as s:
        reset_network(s)
        s.commit()


def test_api_locate_and_save(api_net):
    c, ids = api_net
    assert c.post("/api/auth/login", json={"login": "l_view", "password": PASSWORD}).status_code == 200
    r = c.post("/api/trace", headers=H, json={"port_id": ids["port_a"]})
    assert r.status_code == 200 and r.json()["end_state"] == "port"
    r = c.post("/api/locate", headers=H, json={"cable_id": ids["x"], "fiber": 1, "end": "a", "distance_m": 150})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["interval"]["line"] and body["trace"]["geojson"]["features"]
    assert c.post("/api/locate", headers=H, json={"port_id": ids["port_a"], "distance_m": 150,
                                                  "save": True}).status_code == 403
    assert c.post("/api/auth/login", json={"login": "l_inst", "password": PASSWORD}).status_code == 200
    r = c.post("/api/locate", headers=H, json={"port_id": ids["port_a"], "distance_m": 150, "save": True,
                                               "title": "Обрыв после ДТП"})
    assert r.status_code == 200 and r.json()["incident_id"]
    with SessionLocal() as s:
        inc = s.get(Incident, r.json()["incident_id"])
        assert inc.title == "Обрыв после ДТП" and inc.predicted_error_m > 0 and inc.calc_details["place"]
    r = c.get("/api/crosses", params={"q": "ODF-A"})
    assert r.status_code == 200 and r.json()[0]["used"] == 1
    assert c.post("/api/locate", headers=H, json={"distance_m": 10}).status_code == 422

