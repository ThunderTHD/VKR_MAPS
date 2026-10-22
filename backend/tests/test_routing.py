"""Этап 5: рисование участков с привязкой, поиск пути, прокладка кабеля, очистка тестовой сети."""
import pytest
import sqlalchemy as sa

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import (
    AppUser,
    Cable,
    CableReserve,
    CableRoute,
    CableType,
    Fiber,
    NetworkNode,
    NodeKind,
    Operator,
    Role,
    RouteSegment,
    SegmentKind,
    SourceKind,
)
from app.services import drawing, routing
from app.services.cleanup import clear_generated
from app.services.editing import EditError
from app.services.generator import GeneratorParams, generate_network, reset_network
from app.services.network_ops import refresh_segment_lengths


@pytest.fixture()
def grid(db):
    """A—B—C по прямой (≈100 м + 100 м) и обход A—D—C через точку севернее B.

    Участок C—B нарисован «наоборот» (от C к B), чтобы проверить направление прохода.
    E—F — отдельная трасса, не связанная с остальной сетью.
    """
    db.execute(sa.text("TRUNCATE network_node, route_segment, closure, cable, audit_log RESTART IDENTITY CASCADE"))

    def node(code, lon, lat):
        n = NetworkNode(kind=NodeKind.well, code=code, geom=f"SRID=4326;POINT({lon} {lat})")
        db.add(n)
        db.flush()
        return n

    a, b, c = node("к.№1", 50.0, 55.0), node("к.№2", 50.0016, 55.0), node("к.№3", 50.0032, 55.0)
    d = node("к.№4", 50.0016, 55.001)
    e, f = node("к.№8", 50.01, 55.01), node("к.№9", 50.0116, 55.01)

    def seg(n1, n2):
        p1 = db.execute(sa.text("SELECT ST_X(geom), ST_Y(geom) FROM network_node WHERE id=:i"), {"i": n1.id}).one()
        p2 = db.execute(sa.text("SELECT ST_X(geom), ST_Y(geom) FROM network_node WHERE id=:i"), {"i": n2.id}).one()
        s = RouteSegment(kind=SegmentKind.duct, node_a_id=n1.id, node_b_id=n2.id, geom_length_m=0,
                         geom=f"SRID=4326;LINESTRING({p1[0]} {p1[1]}, {p2[0]} {p2[1]})")
        db.add(s)
        db.flush()
        return s

    segs = {"ab": seg(a, b), "cb": seg(c, b), "ad": seg(a, d), "dc": seg(d, c), "ef": seg(e, f)}
    refresh_segment_lengths(db)
    db.expire_all()
    return {"a": a, "b": b, "c": c, "d": d, "e": e, "f": f, **segs}


def _ref(db):
    op = db.scalar(sa.select(Operator.id).order_by(Operator.id))
    return op, db.scalar(sa.select(CableType).order_by(CableType.id))


# ------------------------------------------------------------------ поиск пути -----

def test_shortest_route_and_directions(db, grid):
    plan = routing.find_route(db, [grid["a"].id, grid["c"].id])
    assert [s.segment_id for s in plan.steps] == [grid["ab"].id, grid["cb"].id]
    assert [s.direction for s in plan.steps] == [1, -1]  # C—B проходим против направления рисования
    assert plan.nodes == [grid["a"].id, grid["b"].id, grid["c"].id]
    assert plan.length_m == pytest.approx(204.6, abs=1.5)


def test_route_through_waypoint(db, grid):
    plan = routing.find_route(db, [grid["a"].id, grid["d"].id, grid["c"].id])
    assert [s.segment_id for s in plan.steps] == [grid["ad"].id, grid["dc"].id]


def test_fact_length_changes_route(db, grid):
    """Вес ребра — лучшая известная длина: если фактическая длина A—B велика, путь идёт в обход."""
    grid["ab"].fact_length_m, grid["ab"].length_source = 500.0, "engineer"
    db.flush()
    plan = routing.find_route(db, [grid["a"].id, grid["c"].id])
    assert [s.segment_id for s in plan.steps] == [grid["ad"].id, grid["dc"].id]


def test_no_path_explained(db, grid):
    with pytest.raises(EditError, match="нет пути"):
        routing.find_route(db, [grid["a"].id, grid["e"].id])
    with pytest.raises(EditError, match="минимум две"):
        routing.find_route(db, [grid["a"].id])


# ------------------------------------------------------------------ кабели -----

def test_create_cable_full(db, grid):
    op, ctype = _ref(db)
    cable = routing.create_cable(db, name="СЛ тест", operator_id=op, cable_type_id=ctype.id,
                                 waypoints=[grid["a"].id, grid["c"].id],
                                 reserves=[{"node_id": grid["b"].id, "length_m": 20}], user_id=None)
    db.expire_all()
    assert db.scalar(sa.select(sa.func.count(Fiber.id)).where(Fiber.cable_id == cable.id)) == ctype.fiber_count
    route = db.scalars(sa.select(CableRoute).where(CableRoute.cable_id == cable.id).order_by(CableRoute.seq)).all()
    assert [(r.segment_id, r.direction) for r in route] == [(grid["ab"].id, 1), (grid["cb"].id, -1)]
    # Геометрия кабеля: от A до C, без разрывов
    ok = db.execute(sa.text("""
        SELECT ST_Equals(ST_StartPoint(c.geom), a.geom) AND ST_Equals(ST_EndPoint(c.geom), z.geom),
               ST_Length(c.geom::geography)
        FROM cable c, network_node a, network_node z WHERE c.id = :c AND a.id = :a AND z.id = :z"""),
        {"c": cable.id, "a": grid["a"].id, "z": grid["c"].id}).one()
    assert ok[0] and ok[1] == pytest.approx(204.6, abs=1.5)
    assert cable.start_node_id == grid["a"].id and cable.end_node_id == grid["c"].id


def test_reserve_must_be_on_route(db, grid):
    op, ctype = _ref(db)
    with pytest.raises(EditError, match="не проходит"):
        routing.create_cable(db, name="СЛ", operator_id=op, cable_type_id=ctype.id,
                             waypoints=[grid["a"].id, grid["c"].id],
                             reserves=[{"node_id": grid["d"].id, "length_m": 10}])


def test_reroute_drops_reserves_off_route(db, grid):
    op, ctype = _ref(db)
    cable = routing.create_cable(db, name="СЛ", operator_id=op, cable_type_id=ctype.id,
                                 waypoints=[grid["a"].id, grid["c"].id],
                                 reserves=[{"node_id": grid["b"].id, "length_m": 15}])
    routing.reroute_cable(db, cable, [grid["a"].id, grid["d"].id, grid["c"].id], user_id=None)
    db.expire_all()
    route = db.scalars(sa.select(CableRoute.segment_id).where(CableRoute.cable_id == cable.id)
                       .order_by(CableRoute.seq)).all()
    assert route == [grid["ad"].id, grid["dc"].id]
    assert db.scalar(sa.select(sa.func.count(CableReserve.id)).where(CableReserve.cable_id == cable.id)) == 0


def test_update_and_delete_cable(db, grid):
    op, ctype = _ref(db)
    cable = routing.create_cable(db, name="СЛ", operator_id=op, cable_type_id=ctype.id,
                                 waypoints=[grid["a"].id, grid["c"].id])
    routing.update_cable(db, cable, {"name": " СЛ 2 ", "twist_factor": 0.008}, user_id=None)
    assert cable.name == "СЛ 2"
    with pytest.raises(EditError):
        routing.update_cable(db, cable, {"twist_factor": 0.5}, user_id=None)
    with pytest.raises(EditError):
        routing.update_cable(db, cable, {"cable_type_id": 1}, user_id=None)
    cid = cable.id
    routing.delete_cable(db, cable, user_id=None)
    assert db.get(Cable, cid) is None


# ------------------------------------------------------------------ рисование -----

def test_draw_segment_snaps_to_existing_node(db, grid):
    # Конец в ~1 м от C — прилипает к C; второй конец далеко — создаётся новый колодец
    seg = drawing.create_segment(db, start={"lon": 50.00321, "lat": 55.000005}, end={"lon": 50.0048, "lat": 55.0},
                                 vertices=[[50.004, 55.0002]], owner_id=None)
    assert seg.node_a_id == grid["c"].id
    new = db.get(NetworkNode, seg.node_b_id)
    assert new.kind == NodeKind.well and new.source_kind == SourceKind.manual
    # Линия начинается ровно в точке C, а не там, где щёлкнули
    exact = db.execute(sa.text("""SELECT ST_Equals(ST_StartPoint(s.geom), n.geom), ST_NPoints(s.geom)
        FROM route_segment s JOIN network_node n ON n.id = s.node_a_id WHERE s.id = :id"""), {"id": seg.id}).one()
    assert exact == (True, 3)
    # Новая трасса сразу участвует в поиске пути
    plan = routing.find_route(db, [grid["a"].id, new.id])
    assert plan.steps[-1].segment_id == seg.id


def test_create_node_refuses_duplicate(db, grid):
    with pytest.raises(EditError, match="уже есть точка"):
        drawing.create_node(db, lon=50.00001, lat=55.0)
    n = drawing.create_node(db, lon=50.002, lat=55.002, kind="pole", code="оп.1")
    assert n.kind == NodeKind.pole


def test_reshape_segment_updates_cable(db, grid):
    op, ctype = _ref(db)
    cable = routing.create_cable(db, name="СЛ", operator_id=op, cable_type_id=ctype.id,
                                 waypoints=[grid["a"].id, grid["c"].id])
    before = grid["ab"].geom_length_m
    drawing.update_segment_geometry(db, grid["ab"], [[50.0008, 55.0005]], user_id=None)
    db.expire_all()
    seg = db.get(RouteSegment, grid["ab"].id)
    assert seg.geom_length_m > before + 20
    npts = db.execute(sa.text("SELECT ST_NPoints(geom) FROM cable WHERE id = :id"), {"id": cable.id}).scalar_one()
    assert npts == 4  # A, изгиб, B, C


def test_split_segment_keeps_cable_routes(db, grid):
    op, ctype = _ref(db)
    grid["ab"].fact_length_m, grid["ab"].length_source = 110.0, "passport"
    db.flush()
    forward = routing.create_cable(db, name="A→C", operator_id=op, cable_type_id=ctype.id,
                                   waypoints=[grid["a"].id, grid["c"].id])
    backward = routing.create_cable(db, name="C→A", operator_id=op, cable_type_id=ctype.id,
                                    waypoints=[grid["c"].id, grid["a"].id])
    ab_id = grid["ab"].id
    node = drawing.split_segment(db, grid["ab"], 50.0008, 55.0, "well", user_id=None)
    new_seg = db.scalar(sa.select(RouteSegment.id).where(RouteSegment.node_a_id == node.id))
    for cable, expected in ((forward, [(ab_id, 1), (new_seg, 1), (grid["cb"].id, -1)]),
                            (backward, [(grid["cb"].id, 1), (new_seg, -1), (ab_id, -1)])):
        route = db.execute(sa.select(CableRoute.segment_id, CableRoute.direction)
                           .where(CableRoute.cable_id == cable.id).order_by(CableRoute.seq)).all()
        assert [tuple(r) for r in route] == expected
        # Геометрия кабеля по-прежнему непрерывна и проходит через новую точку
        through = db.execute(sa.text("""SELECT ST_DWithin(c.geom, n.geom, 1e-9) FROM cable c, network_node n
            WHERE c.id = :c AND n.id = :n"""), {"c": cable.id, "n": node.id}).scalar_one()
        assert through
    lens = db.execute(sa.select(RouteSegment.fact_length_m).where(RouteSegment.id.in_([ab_id, new_seg]))).scalars()
    assert sum(lens) == pytest.approx(110.0)


# ------------------------------------------------------------------ очистка -----

def test_clear_generated_keeps_imported(db):
    from app.models import ImportJob, ImportKind
    from app.services.kml.importer import commit_plan, plan_import, suggest_mapping
    from app.services.kml.parser import parse_kml
    from tests.kml_factory import sample_kml

    generate_network(db, GeneratorParams(nodes=40, seed=3, buildings_per_operator=2))
    doc = parse_kml(sample_kml())
    mapping = suggest_mapping(db, doc)
    job = ImportJob(kind=ImportKind.kml, filename="t.kml")
    db.add(job)
    db.flush()
    # Синтетическая KML — в другом месте карты, чем тестовая сеть: привязок к тестовым узлам нет
    commit_plan(db, plan_import(db, doc, mapping), job_id=job.id, user_id=None, mapping=mapping)
    imported = db.scalar(sa.select(sa.func.count(NetworkNode.id))
                         .where(NetworkNode.source_kind != SourceKind.generator))
    result = clear_generated(db)
    assert result["deleted"]["кабели"] > 0 and result["kept_nodes"] == 0
    assert db.scalar(sa.select(sa.func.count(Cable.id))) == 0
    assert db.scalar(sa.select(sa.func.count(NetworkNode.id))) == imported > 0


# ------------------------------------------------------------------ API -----

PASSWORD = "Тест-пароль-5"
H = {"X-Requested-With": "fetch"}


@pytest.fixture()
def admin_client(client):
    with SessionLocal() as s:
        reset_network(s)
        s.execute(sa.delete(AppUser).where(AppUser.login.in_(["r_admin", "r_viewer"])))
        for login, role in (("r_admin", Role.admin), ("r_viewer", Role.viewer)):
            s.add(AppUser(login=login, full_name=login, role=role, password_hash=hash_password(PASSWORD)))
        s.commit()
    yield client
    with SessionLocal() as s:
        reset_network(s)
        s.commit()


def _login(client, who):
    assert client.post("/api/auth/login", json={"login": who, "password": PASSWORD}).status_code == 200


def test_api_draw_and_lay_cable(admin_client):
    c = admin_client
    _login(c, "r_admin")
    r = c.post("/api/segments", headers=H,
               json={"start": {"lon": 49.1, "lat": 55.8}, "end": {"lon": 49.102, "lat": 55.8}})
    assert r.status_code == 200, r.text
    s1 = r.json()
    # Второй участок начинается у конца первого (щелчок в ~0,5 м) — привязка
    r = c.post("/api/segments", headers=H, json={"start": {"lon": 49.102005, "lat": 55.8},
                                                 "end": {"lon": 49.102, "lat": 55.801}, "kind": "aerial",
                                                 "new_node_kind": "pole"})
    s2 = r.json()
    assert s2["node_a_id"] == s1["node_b_id"]
    a, z = s1["node_a_id"], s2["node_b_id"]
    r = c.post("/api/routing/path", headers=H, json={"waypoints": [a, z]})
    assert r.status_code == 200 and len(r.json()["segments"]) == 2
    assert r.json()["geojson"]["features"][0]["geometry"]["type"] == "LineString"
    ref = c.get("/api/map/reference").json()
    body = {"name": "СЛ API", "operator_id": ref["operators"][0]["id"], "cable_type_id": ref["cable_types"][0]["id"],
            "waypoints": [a, z], "reserves": [{"node_id": s1["node_b_id"], "length_m": 10}]}
    r = c.post("/api/cables", headers=H, json=body)
    assert r.status_code == 200, r.text
    cable_id = r.json()["id"]
    card = c.get(f"/api/objects/cable/{cable_id}").json()
    assert "СЛ API" in str(card)
    r = c.put(f"/api/segments/{s1['id']}/geometry", headers=H, json={"vertices": [[49.101, 55.8003]]})
    assert r.status_code == 200 and r.json()["length_m"] > 0
    # Ошибка предметной области — 409 с понятным текстом
    r = c.post("/api/routing/path", headers=H, json={"waypoints": [a, a]})
    assert r.status_code == 409 and "два раза" in r.json()["detail"]
    assert c.delete(f"/api/cables/{cable_id}", headers=H).status_code == 200


def test_api_drawing_admin_only(admin_client):
    _login(admin_client, "r_viewer")
    r = admin_client.post("/api/nodes", headers=H, json={"lon": 49.1, "lat": 55.8})
    assert r.status_code == 403
