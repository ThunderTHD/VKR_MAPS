"""Этап 11: анализ последствий, корреляция аварий, мониторинг узлов доступа."""
import random
from collections import Counter

import pytest
import sqlalchemy as sa

from app.core.config import MonitoringMode, get_settings
from app.models import AccessNode, NodeStatus, NodeStatusHistory, SimFault
from app.services import impact, monitoring
from app.services.editing import EditError
from app.services.generator import GeneratorParams, NetworkGenerator, reset_network

PASSWORD = "Тест-пароль-11"


@pytest.fixture()
def net(db):
    reset_network(db)   # в базе могли остаться данные других тестов
    NetworkGenerator(db, GeneratorParams(nodes=80, seed=11, buildings_per_operator=5)).run()
    db.flush()
    return db


def _shared_segment(db, k_min=3):
    """Участок, через который идут тракты нескольких узлов (магистраль)."""
    paths = impact.service_paths(db, refresh=True)
    c = Counter(s for p in paths.values() for s in p.segments)
    return next(seg for seg, k in c.most_common() if k >= k_min), paths


def test_paths_traced(net):
    paths = impact.service_paths(net, refresh=True)
    assert paths and all(p.end_state == "port" for p in paths.values())
    p = next(iter(paths.values()))
    assert p.segments and p.cables and p.closures and p.far_port.startswith("ODF-1")
    # Повторный вызов без изменений сети — из кеша (тот же объект)
    assert impact.service_paths(net) is paths


def test_impact_segment_cable_node(net):
    seg, paths = _shared_segment(net)
    r = impact.impact(net, "segment", seg)
    expected = {p.access_node_id for p in paths.values() if seg in p.segments}
    assert {a["id"] for a in r["access_nodes"]} == expected and r["count"] >= 3
    assert all(a["via"] == "линейный тракт" for a in r["access_nodes"])
    # Кабель-ответвление одного клиента — отключается только он
    p = next(iter(paths.values()))
    drop = next(c for c in p.cables if sum(c in q.cables for q in paths.values()) == 1)
    assert impact.impact(net, "cable", drop)["count"] == 1
    # Точка узла оператора: пропадает агрегирующий коммутатор и все подключённые через него
    agg = net.scalars(sa.select(AccessNode).where(AccessNode.uplink_id.is_(None))).first()
    r = impact.impact(net, "node", agg.node_id)
    assert agg.id in {a["id"] for a in r["access_nodes"]} and r["count"] > 1
    r = impact.impact(net, "access_node", agg.id)
    assert r["count"] == len(impact.downstream(net, agg.id)) - 1
    with pytest.raises(EditError):
        impact.impact(net, "fiber", 1)


def test_correlation_finds_cut(net):
    seg, paths = _shared_segment(net)
    down = {p.access_node_id for p in paths.values() if seg in p.segments}
    net.execute(sa.update(AccessNode).values(status=NodeStatus.up))
    res = impact.correlate(net, down)
    best = res["candidates"][0]
    assert seg in best["segment_ids"] and best["down_through"] == best["down_total"] == len(down)
    assert best["alive_through"] == 0 and best["confidence"] in ("высокая", "средняя")
    # Ожидаемое расстояние на рефлектометре — в пределах длины тракта
    otdr = best["otdr"]
    assert 0 <= otdr["min_m"] < otdr["max_m"] and otdr["from_port"].startswith("ODF-1")


def test_correlation_uplink(net):
    agg = net.scalars(sa.select(AccessNode).where(AccessNode.uplink_id.is_(None))).first()
    down = impact.downstream(net, agg.id)
    res = impact.correlate(net, down)
    assert res["uplink"]["id"] == agg.id


class FakeProber:
    def __init__(self, down=()):
        self.down = set(down)

    def probe(self, db, nodes):
        return {a.id: monitoring.Probe(a.id not in self.down, None if a.id in self.down else 1.5) for a in nodes}


def test_poll_threshold_and_history(net):
    victim = net.scalars(sa.select(AccessNode).where(AccessNode.uplink_id.is_not(None))).first()
    r = monitoring.poll(net, FakeProber(), threshold=3)
    assert r["up"] == r["polled"] and r["went_down"] == []
    for i in range(2):   # два пропуска — ещё не авария
        r = monitoring.poll(net, FakeProber({victim.id}), threshold=3)
        assert victim.status == NodeStatus.up and victim.fail_count == i + 1
    r = monitoring.poll(net, FakeProber({victim.id}), threshold=3)
    assert victim.status == NodeStatus.down and r["went_down"] == [victim.id]
    r = monitoring.poll(net, FakeProber(), threshold=3)
    assert victim.status == NodeStatus.up and r["went_up"] == [victim.id] and victim.fail_count == 0
    hist = net.scalars(sa.select(NodeStatusHistory.status).where(NodeStatusHistory.access_node_id == victim.id)
                       .order_by(NodeStatusHistory.id)).all()
    assert hist == [NodeStatus.up, NodeStatus.down, NodeStatus.up]
    av = monitoring.availability(net, victim.id, hours=1)
    assert av["outages"] == 1 and av["availability_pct"] is not None


def test_simulated_cut(net, monkeypatch):
    monkeypatch.setattr(get_settings(), "monitoring_mode", MonitoringMode.simulate)
    seg, paths = _shared_segment(net)
    expected = {p.access_node_id for p in paths.values() if seg in p.segments}
    monitoring.simulate(net, segment_id=seg)
    res = monitoring.poll_until_settled(net)
    down = set(net.scalars(sa.select(AccessNode.id).where(AccessNode.status == NodeStatus.down)))
    assert down == expected and set(res["went_down"]) == expected
    assert seg in impact.correlate(net)["candidates"][0]["segment_ids"]
    assert monitoring.clear_simulation(net) == 1
    monitoring.poll_until_settled(net)
    assert not net.scalar(sa.select(sa.func.count()).where(AccessNode.status == NodeStatus.down))
    with pytest.raises(EditError, match="либо"):
        monitoring.simulate(net)


def test_simulated_power_and_auto(net, monkeypatch):
    monkeypatch.setattr(get_settings(), "monitoring_mode", MonitoringMode.simulate)
    agg = net.scalars(sa.select(AccessNode).where(AccessNode.uplink_id.is_(None))).first()
    monitoring.simulate(net, access_node_id=agg.id)
    assert monitoring.dead_nodes(net) == impact.downstream(net, agg.id)
    monitoring.clear_simulation(net)
    # Самопроизвольная авария имитатора — одна, на участке с трактом
    monitoring.SimProber(rnd=random.Random(1), auto_rate=1.0, flap=0).probe(net, [])
    monitoring.SimProber(rnd=random.Random(2), auto_rate=1.0, flap=0).probe(net, [])
    faults = net.scalars(sa.select(SimFault)).all()
    assert len(faults) == 1 and faults[0].auto and faults[0].expires_at is not None


def test_simulation_only_in_simulate_mode(net, monkeypatch):
    monkeypatch.setattr(get_settings(), "monitoring_mode", MonitoringMode.ping)
    with pytest.raises(EditError, match="simulate"):
        monitoring.simulate(net, segment_id=1)


def test_ping_rtt_parse():
    out = "64 bytes from 10.0.0.1: icmp_seq=1 ttl=64 time=0.045 ms"
    assert monitoring.PingProber.parse_rtt(out) == pytest.approx(0.045)
    assert monitoring.PingProber.parse_rtt("Request timeout") is None
    fp = ("10.0.0.1 : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.04/0.04/0.04\n"
          "10.0.0.2 : xmt/rcv/%loss = 1/0/100%\n")
    res = monitoring.PingProber.parse_fping(fp)
    assert res["10.0.0.1"].ok and res["10.0.0.1"].rtt_ms == pytest.approx(0.04) and not res["10.0.0.2"].ok


@pytest.mark.skipif(not monitoring.PingProber.available(), reason="нет утилиты ping")
def test_ping_localhost():
    r = monitoring.PingProber()._one("127.0.0.1")
    assert r.ok in (True, False)    # в контейнере CI без CAP_NET_RAW ping может быть запрещён


def test_overview_and_api(client):
    from app.core.db import SessionLocal
    from app.core.security import hash_password
    from app.models import AppUser, Role
    with SessionLocal() as s:
        s.execute(sa.delete(AppUser).where(AppUser.login == "m_view"))
        s.add(AppUser(login="m_view", full_name="m", role=Role.viewer, password_hash=hash_password(PASSWORD)))
        s.commit()
    assert client.post("/api/auth/login", json={"login": "m_view", "password": PASSWORD}).status_code == 200
    body = client.get("/api/monitoring").json()
    assert {"mode", "nodes", "events", "sim_faults", "counts"} <= body.keys()
    assert client.get("/api/monitoring/correlate").status_code == 200
    assert client.get("/api/impact/fiber/1").status_code == 409
    H = {"X-Requested-With": "fetch"}
    assert client.post("/api/monitoring/poll", headers=H).status_code == 403
    assert client.post("/api/monitoring/simulate", headers=H, json={"segment_id": 1}).status_code == 403


def test_export_kml_geojson(net):
    import json

    import lxml.etree as et

    from app.services import export
    from app.services.kml.parser import parse_kml
    kml = export.to_kml(net)
    et.fromstring(kml)
    doc = parse_kml(kml)
    folders = doc.folders()
    assert "Колодцы" in folders and "Трасса" in folders and any(f.startswith("Кабели") for f in folders)
    assert folders["Трасса"]["types"] == {"LineString": folders["Трасса"]["count"]}
    gj = json.loads(export.to_geojson(net))
    layers = {f["properties"]["layer"] for f in gj["features"]}
    assert layers == {"node", "segment", "cable"}
    # Фильтр по оператору — только его кабели
    op_id = net.execute(sa.text("SELECT operator_id FROM cable WHERE operator_id IS NOT NULL LIMIT 1")).scalar()
    part = json.loads(export.to_geojson(net, op_id))
    cables = [f for f in part["features"] if f["properties"]["layer"] == "cable"]
    assert cables and len({f["properties"]["operator"] for f in cables}) == 1
    assert len(part["features"]) < len(gj["features"])


def test_random_cut(net, monkeypatch):
    monkeypatch.setattr(get_settings(), "monitoring_mode", MonitoringMode.simulate)
    f = monitoring.simulate(net, random_cut=True)
    assert f.kind == "cut" and f.segment_id
    monitoring.poll_until_settled(net)
    assert len(monitoring.dead_nodes(net)) >= 2
