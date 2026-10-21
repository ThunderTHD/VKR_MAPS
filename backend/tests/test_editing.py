"""Редактирование администратором: правка, перенос, объединение, удаление, переклассификация, журнал."""
import pytest
import sqlalchemy as sa

from app.models import (
    AuditLog,
    Cable,
    CableRoute,
    CableType,
    Closure,
    NetworkNode,
    NodeKind,
    Operator,
    RouteSegment,
    SegmentKind,
    UnclassifiedFeature,
)
from app.services import editing
from app.services.editing import EditError
from app.services.network_ops import make_fibers, refresh_segment_lengths


@pytest.fixture()
def net(db):
    """Три колодца в ряд (A—B—C через 100 м) и дубль B рядом с B, к дублю ведёт ещё участок."""
    db.execute(sa.text("TRUNCATE network_node, route_segment, closure, cable, unclassified_feature, audit_log "
                       "RESTART IDENTITY CASCADE"))

    def node(code, lon, lat, kind=NodeKind.well):
        n = NetworkNode(kind=kind, code=code, geom=f"SRID=4326;POINT({lon} {lat})")
        db.add(n)
        db.flush()
        return n

    a, b, c = node("к.№1", 50.0, 55.0), node("к.№2", 50.0016, 55.0), node("к.№3", 50.0032, 55.0)
    b2 = node(None, 50.00161, 55.00001, NodeKind.unknown)  # дубль B (~1 м), без кода

    def seg(n1, n2, x1, x2):
        s = RouteSegment(kind=SegmentKind.duct, node_a_id=n1.id, node_b_id=n2.id, geom_length_m=0,
                         geom=f"SRID=4326;LINESTRING({x1} 55.0, {x2} 55.0)")
        db.add(s)
        db.flush()
        return s

    ab = seg(a, b, 50.0, 50.0016)
    b2c = seg(b2, c, 50.00161, 50.0032)
    refresh_segment_lengths(db)
    db.add(Closure(node_id=b2.id, number="М-дубль"))
    db.flush()
    return {"a": a, "b": b, "c": c, "b2": b2, "ab": ab, "b2c": b2c}


def test_update_node_writes_audit(db, net):
    editing.update_node(db, net["a"], {"name": "Колодец у школы", "code": " к.№1 "}, user_id=None)
    assert net["a"].name == "Колодец у школы" and net["a"].code == "к.№1"
    log = db.scalar(sa.select(AuditLog).where(AuditLog.entity_type == "network_node"))
    assert log.action == "update" and log.before["name"] is None and log.after["name"] == "Колодец у школы"


def test_update_node_rejects_unknown_field(db, net):
    with pytest.raises(EditError):
        editing.update_node(db, net["a"], {"geom": "x"}, user_id=None)


def test_move_node_moves_segment_ends(db, net):
    before = net["ab"].geom_length_m
    editing.move_node(db, net["b"], 50.0024, 55.0, user_id=None)  # отодвинули на ~50 м
    db.expire_all()
    seg = db.get(RouteSegment, net["ab"].id)
    end = db.execute(sa.text("SELECT ST_X(ST_EndPoint(geom)) FROM route_segment WHERE id = :id"),
                     {"id": seg.id}).scalar_one()
    assert end == pytest.approx(50.0024)
    assert seg.geom_length_m > before + 40


def test_merge_nodes_relinks_everything(db, net):
    result = editing.merge_nodes(db, net["b"], net["b2"], user_id=None)
    assert result["segments_relinked"] == 1
    db.expire_all()
    b2c = db.get(RouteSegment, net["b2c"].id)
    assert b2c.node_a_id == net["b"].id
    assert db.get(NetworkNode, net["b2"].id) is None
    assert db.scalar(sa.select(Closure.node_id)) == net["b"].id
    # Начало участка перенесено точно в координаты сохранённого узла
    same = db.execute(sa.text("""SELECT ST_Equals(ST_StartPoint(s.geom), n.geom)
        FROM route_segment s JOIN network_node n ON n.id = s.node_a_id WHERE s.id = :id"""),
        {"id": b2c.id}).scalar_one()
    assert same
    assert db.scalar(sa.select(AuditLog.action).where(AuditLog.action == "merge")) == "merge"


def test_merge_refuses_cable_loop(db, net):
    op = db.scalar(sa.select(Operator))
    ct = db.scalar(sa.select(CableType).where(CableType.mark.like("ОКЛ-8%")))
    cable = Cable(name="К", operator_id=op.id, cable_type_id=ct.id, fibers=make_fibers(ct),
                  route=[CableRoute(seq=1, segment_id=net["ab"].id, direction=1)])
    db.add(cable)
    db.flush()
    with pytest.raises(EditError, match="петлёй"):
        editing.merge_nodes(db, net["a"], net["b"], user_id=None)


def test_merge_removes_empty_loop(db, net):
    result = editing.merge_nodes(db, net["a"], net["b"], user_id=None)
    assert result["loops_removed"] == 1
    db.expire_all()
    assert db.get(RouteSegment, net["ab"].id) is None


def test_delete_node_only_if_unused(db, net):
    with pytest.raises(EditError, match="участки трасс"):
        editing.delete_node(db, net["a"], user_id=None)
    lonely = NetworkNode(kind=NodeKind.unknown, geom="SRID=4326;POINT(50.1 55.1)")
    db.add(lonely)
    db.flush()
    editing.delete_node(db, lonely, user_id=None)
    assert db.get(NetworkNode, lonely.id) is None


def test_segment_length_requires_source(db, net):
    with pytest.raises(EditError, match="источник"):
        editing.update_segment(db, net["ab"], {"fact_length_m": 105.0}, user_id=None)
    editing.update_segment(db, net["ab"], {"fact_length_m": 105.0, "length_source": "engineer"}, user_id=None)
    assert net["ab"].fact_length_m == 105.0 and net["ab"].length_updated_at is not None


def test_installer_limited_fields(db, net):
    allowed = {"fact_length_m", "length_source", "length_note"}
    with pytest.raises(EditError):
        editing.update_segment(db, net["ab"], {"owner_id": 1}, user_id=None, allowed=allowed)


def test_classify_line_to_segment(db, net):
    feat = UnclassifiedFeature(folder="NEC", name="57110 - 56760",
                               geom="SRID=4326;LINESTRING(50.0032 55.0, 50.006 55.001)")
    db.add(feat)
    db.flush()
    result = editing.classify_feature(db, feat, "segment", user_id=None, segment_kind="aerial")
    seg = db.get(RouteSegment, result["segment_id"])
    assert seg.kind == SegmentKind.aerial
    assert seg.node_a_id == net["c"].id                 # начало привязалось к существующему колодцу
    assert db.get(NetworkNode, seg.node_b_id).kind == NodeKind.unknown  # конец — новая точка
    assert seg.geom_length_m > 150
    assert db.get(UnclassifiedFeature, feat.id) is None


def test_classify_point_to_node(db, net):
    feat = UnclassifiedFeature(folder="NEC", name="Опора 5", geom="SRID=4326;POINT(50.01 55.01)")
    db.add(feat)
    db.flush()
    result = editing.classify_feature(db, feat, "node", user_id=None, node_kind="pole")
    assert db.get(NetworkNode, result["node_id"]).kind == NodeKind.pole
