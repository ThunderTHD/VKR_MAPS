"""Генератор тестовой сети: связность, полнота трактов, «истина» для эксперимента."""
import networkx as nx
import pytest
import sqlalchemy as sa

from app.core.security import hash_password, verify_password
from app.models import (
    AccessNode,
    Cable,
    CrossPort,
    Measurement,
    NetworkNode,
    RouteSegment,
    SimTruth,
)
from app.services.generator import GeneratorParams, NetworkGenerator, reset_network


@pytest.fixture()
def generated(db):
    reset_network(db)
    gen = NetworkGenerator(db, GeneratorParams(nodes=80, seed=7, buildings_per_operator=4))
    report = gen.run()
    return gen, report


def test_generator_counts(generated):
    _, report = generated
    c = report.counts
    assert c["кабели"] > 8 and c["муфты"] >= 4 and c["сварки"] > 50
    assert c["узлы доступа"] >= 8


def test_infrastructure_is_connected(db, generated):
    g = nx.Graph()
    g.add_edges_from(db.execute(sa.select(RouteSegment.node_a_id, RouteSegment.node_b_id)).all())
    assert g.number_of_nodes() == db.scalar(sa.select(sa.func.count(NetworkNode.id)))
    assert nx.is_connected(g)


def test_segment_lengths_from_geography(db, generated):
    """geom_length_m пересчитан в базе по эллипсоиду и близок к сферической оценке."""
    rows = db.execute(sa.text(
        "SELECT geom_length_m, ST_Length(geom::geography) FROM route_segment")).all()
    assert all(abs(a - b) < 0.01 for a, b in rows)


def test_cable_route_is_continuous(db, generated):
    """Участки маршрута кабеля стыкуются конец в конец с учётом направления."""
    for cable in db.scalars(sa.select(Cable)):
        prev_end = cable.start_node_id
        for step in cable.route:
            seg = step.segment
            start, end = (seg.node_a_id, seg.node_b_id) if step.direction == 1 else (seg.node_b_id, seg.node_a_id)
            assert start == prev_end, f"разрыв маршрута кабеля {cable.name}"
            prev_end = end
        assert prev_end == cable.end_node_id


def test_every_access_node_is_traceable(db, generated):
    """От порта кросса каждого здания можно дойти по сваркам до кросса узла оператора."""
    gen, _ = generated
    truth = {(t.entity_type, t.entity_id, t.attribute): t.value for t in db.scalars(sa.select(SimTruth))}
    site_ports = db.scalars(sa.select(CrossPort).where(CrossPort.fiber_id.is_not(None))).all()
    for an in db.scalars(sa.select(AccessNode).where(AccessNode.cross_port_id.is_not(None))):
        reached = [p for p in site_ports
                   if p.id != an.cross_port_id and gen._true_optical_length(p.id, an.cross_port_id, truth)]
        assert len(reached) == 1, f"тракт до {an.hostname} не найден"


def test_measurements_close_to_truth(db, generated):
    """Измерение «из ИД» отличается от истинной оптической длины не более чем на несколько метров."""
    truth = {t.entity_id: t.value for t in db.scalars(
        sa.select(SimTruth).where(SimTruth.entity_type == "measurement"))}
    measurements = db.scalars(sa.select(Measurement)).all()
    assert measurements
    for m in measurements:
        assert abs(m.optical_length_m - truth[m.id]) < 5


def test_recorded_lengths_have_source_errors(db, generated):
    """Записанные длины отличаются от истины, но в пределах погрешности источника (с запасом)."""
    rows = db.execute(sa.text("""
        SELECT s.length_source, s.fact_length_m / t.value - 1
        FROM route_segment s JOIN sim_truth t
          ON t.entity_type = 'route_segment' AND t.entity_id = s.id
        WHERE s.fact_length_m IS NOT NULL""")).all()
    limits = {"reflectometer": 0.02, "as_built": 0.04, "passport": 0.10, "engineer": 0.20}
    assert rows
    assert all(abs(err) <= limits[src] for src, err in rows)


def test_same_seed_same_network(db):
    reset_network(db)
    NetworkGenerator(db, GeneratorParams(nodes=40, seed=3, buildings_per_operator=2)).run()
    first = db.execute(sa.text("SELECT ST_AsText(geom) FROM network_node ORDER BY id")).scalars().all()
    reset_network(db)
    NetworkGenerator(db, GeneratorParams(nodes=40, seed=3, buildings_per_operator=2)).run()
    second = db.execute(sa.text("SELECT ST_AsText(geom) FROM network_node ORDER BY id")).scalars().all()
    assert first == second


def test_password_hashing():
    h = hash_password("секрет-123")
    assert h.startswith("scrypt$") and "секрет" not in h
    assert verify_password("секрет-123", h)
    assert not verify_password("секрет-124", h)
    assert hash_password("секрет-123") != h  # соль у каждого хэша своя
    assert not verify_password("x", "мусор")


def test_stats_endpoint_requires_login(client):
    assert client.get("/api/stats").status_code == 401
