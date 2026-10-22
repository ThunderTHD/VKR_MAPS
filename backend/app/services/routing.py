"""Прокладка кабеля по трассе: поиск пути по графу участков.

Граф трасс: вершины — точки сети (колодцы, опоры, узлы), рёбра — участки трасс.
Вес ребра — лучшая известная длина участка (фактическая, иначе по геометрии), поэтому
кратчайший путь — это путь минимальной длины кабеля, а не минимального числа пролётов.

Пользователь задаёт опорные точки (начало, промежуточные колодцы, конец); между каждой
парой соседних точек ищется кратчайший путь (алгоритм Дейкстры), отрезки склеиваются.
Промежуточные точки нужны, когда кабель идёт не кратчайшим путём (например, по
определённой канализации или в обход).
"""
from dataclasses import dataclass, field

import networkx as nx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    Cable,
    CableReserve,
    CableRoute,
    CableType,
    LengthSourceCode,
    NetworkNode,
    Operator,
    SourceKind,
)
from app.services.editing import EditError, audit
from app.services.network_ops import make_fibers, refresh_cable_geometry


@dataclass
class RouteStep:
    segment_id: int
    direction: int      # +1: от node_a к node_b, -1: обратно
    from_node: int
    to_node: int
    length_m: float
    known: bool         # длина фактическая (True) или по геометрии (False)


@dataclass
class RoutePlan:
    steps: list[RouteStep] = field(default_factory=list)

    @property
    def length_m(self) -> float:
        return sum(s.length_m for s in self.steps)

    @property
    def nodes(self) -> list[int]:
        return [self.steps[0].from_node] + [s.to_node for s in self.steps] if self.steps else []


def build_graph(db: Session, kinds: list[str] | None = None) -> nx.MultiGraph:
    """Граф трасс. MultiGraph: между двумя колодцами может быть несколько участков (две канализации)."""
    q = sa.text("""
        SELECT id, node_a_id, node_b_id, coalesce(fact_length_m, geom_length_m) AS len,
               fact_length_m IS NOT NULL AS known, kind
        FROM route_segment""")
    g = nx.MultiGraph()
    for seg_id, a, b, length, known, kind in db.execute(q):
        if kinds and kind not in kinds:
            continue
        g.add_edge(a, b, key=seg_id, weight=max(float(length), 0.01), known=known)
    return g


def find_route(db: Session, waypoints: list[int], graph: nx.MultiGraph | None = None) -> RoutePlan:
    """Кратчайший путь по трассам через опорные точки (по порядку)."""
    if len(waypoints) < 2:
        raise EditError("Нужно минимум две точки: начало и конец кабеля")
    if any(a == b for a, b in zip(waypoints, waypoints[1:], strict=False)):
        raise EditError("Одна и та же точка указана два раза подряд")
    g = graph if graph is not None else build_graph(db)
    plan = RoutePlan()
    for start, end in zip(waypoints, waypoints[1:], strict=False):
        if start not in g or end not in g:
            missing = start if start not in g else end
            raise EditError(f"К точке {_label(db, missing)} не подходит ни один участок трассы")
        try:
            path = nx.dijkstra_path(g, start, end, weight="weight")
        except nx.NetworkXNoPath:
            raise EditError(f"Между {_label(db, start)} и {_label(db, end)} нет пути по трассам — "
                            f"сначала нарисуйте недостающие участки") from None
        for u, v in zip(path, path[1:], strict=False):
            # Из параллельных участков берём самый короткий
            key, data = min(g.get_edge_data(u, v).items(), key=lambda kv: kv[1]["weight"])
            plan.steps.append(RouteStep(key, 0, u, v, data["weight"], data["known"]))
    # Направление прохода по каждому участку
    ends = dict(db.execute(sa.text("SELECT id, node_a_id FROM route_segment WHERE id = ANY(:ids)"),
                           {"ids": [s.segment_id for s in plan.steps]}).all())
    for s in plan.steps:
        s.direction = 1 if ends[s.segment_id] == s.from_node else -1
    return plan


def _label(db: Session, node_id: int) -> str:
    """Подпись точки для сообщений: код, иначе название, иначе id."""
    row = db.execute(sa.text("SELECT coalesce(code, name) FROM network_node WHERE id = :id"), {"id": node_id}).first()
    return f"«{row[0]}»" if row and row[0] else f"id={node_id}"


def route_geojson(db: Session, plan: RoutePlan) -> dict:
    """Предпросмотр маршрута: участки и общая длина."""
    ids = [s.segment_id for s in plan.steps]
    feats = db.execute(sa.text("""
        SELECT id, ST_AsGeoJSON(geom, 7)::json FROM route_segment WHERE id = ANY(:ids)"""), {"ids": ids}).all()
    geom = dict(feats)
    return {"type": "FeatureCollection", "features": [
        {"type": "Feature", "geometry": geom[s.segment_id],
         "properties": {"seq": i, "segment_id": s.segment_id, "length_m": round(s.length_m, 1), "known": s.known}}
        for i, s in enumerate(plan.steps, 1)]}


def check_route_repeats(plan: RoutePlan) -> list[str]:
    """Предупреждения: кабель проходит один участок дважды (петля через опорные точки)."""
    seen, warnings = set(), []
    for s in plan.steps:
        if s.segment_id in seen:
            warnings.append(f"Участок id={s.segment_id} в маршруте дважды — проверьте опорные точки")
        seen.add(s.segment_id)
    return warnings


def create_cable(db: Session, *, name: str, operator_id: int, cable_type_id: int, waypoints: list[int],
                 twist_factor: float = 0.005, lay_year: int | None = None, note: str | None = None,
                 reserves: list[dict] | None = None, user_id: int | None = None) -> Cable:
    """Создать кабель по трассе: волокна по марке, маршрут, запасы, геометрия."""
    if not name.strip():
        raise EditError("Укажите название кабеля")
    ctype = db.get(CableType, cable_type_id)
    if ctype is None or db.get(Operator, operator_id) is None:
        raise EditError("Неизвестный оператор или марка кабеля")
    plan = find_route(db, waypoints)
    if check_route_repeats(plan):
        raise EditError("Маршрут проходит один участок дважды — уберите лишнюю опорную точку")
    nodes = plan.nodes
    cable = Cable(name=name.strip(), operator_id=operator_id, cable_type_id=ctype.id, twist_factor=twist_factor,
                  start_node_id=nodes[0], end_node_id=nodes[-1], lay_year=lay_year, note=note,
                  source_kind=SourceKind.manual)
    cable.fibers = make_fibers(ctype)
    cable.route = [CableRoute(seq=i, segment_id=s.segment_id, direction=s.direction)
                   for i, s in enumerate(plan.steps, 1)]
    db.add(cable)
    db.flush()
    on_route = set(nodes)
    for r in reserves or []:
        if r["node_id"] not in on_route:
            raise EditError(f"Запас указан в точке id={r['node_id']}, через которую кабель не проходит")
        if r["length_m"] <= 0:
            raise EditError("Длина запаса должна быть больше нуля")
        db.add(CableReserve(cable_id=cable.id, node_id=r["node_id"], length_m=r["length_m"],
                            length_source=LengthSourceCode(r.get("source") or "engineer")))
    db.flush()
    refresh_cable_geometry(db, [cable.id])
    audit(db, user_id, "create", "cable", cable.id, None,
          {"name": cable.name, "operator_id": operator_id, "cable_type_id": ctype.id, "segments": len(plan.steps),
           "length_m": round(plan.length_m, 1)})
    return cable


def reroute_cable(db: Session, cable: Cable, waypoints: list[int], user_id: int | None) -> Cable:
    """Изменить маршрут кабеля (концы могут сместиться — тогда проверяем, что концы не заняты сваркой)."""
    plan = find_route(db, waypoints)
    if check_route_repeats(plan):
        raise EditError("Маршрут проходит один участок дважды — уберите лишнюю опорную точку")
    nodes = plan.nodes
    if (nodes[0], nodes[-1]) != (cable.start_node_id, cable.end_node_id) and _has_connections(db, cable):
        raise EditError("Концы кабеля сварены в муфтах или заведены на кросс — менять начало и конец нельзя, "
                        "можно только путь между ними")
    before = {"segments": [r.segment_id for r in cable.route], "start": cable.start_node_id, "end": cable.end_node_id}
    cable.route.clear()
    db.flush()
    cable.route.extend(CableRoute(seq=i, segment_id=s.segment_id, direction=s.direction)
                       for i, s in enumerate(plan.steps, 1))
    cable.start_node_id, cable.end_node_id = nodes[0], nodes[-1]
    # Запасы в точках, через которые кабель больше не идёт, теряют смысл
    for r in list(cable.reserves):
        if r.node_id not in set(nodes):
            cable.reserves.remove(r)
    db.flush()
    refresh_cable_geometry(db, [cable.id])
    audit(db, user_id, "reroute", "cable", cable.id, before,
          {"segments": [s.segment_id for s in plan.steps], "start": nodes[0], "end": nodes[-1]})
    return cable


def _has_connections(db: Session, cable: Cable) -> bool:
    return bool(db.execute(sa.text("""
        SELECT 1 FROM fiber f WHERE f.cable_id = :id AND (
            EXISTS (SELECT 1 FROM splice s WHERE f.id IN (s.fiber_a_id, s.fiber_b_id))
            OR EXISTS (SELECT 1 FROM cross_port p WHERE p.fiber_id = f.id)) LIMIT 1"""), {"id": cable.id}).first())


CABLE_FIELDS = ["name", "operator_id", "twist_factor", "lay_year", "note"]


def update_cable(db: Session, cable: Cable, changes: dict, user_id: int | None) -> Cable:
    before = {f: getattr(cable, f) for f in CABLE_FIELDS}
    for k, v in changes.items():
        if k not in CABLE_FIELDS:
            raise EditError(f"Поле «{k}» менять нельзя")
        if k == "twist_factor" and v is not None and not 0 <= v < 0.2:
            raise EditError("Коэффициент укрутки — от 0 до 0,2 (обычно 0,003–0,012)")
        setattr(cable, k, v.strip() if isinstance(v, str) else v)
    db.flush()
    audit(db, user_id, "update", "cable", cable.id, before, {f: getattr(cable, f) for f in CABLE_FIELDS})
    return cable


def delete_cable(db: Session, cable: Cable, user_id: int | None) -> None:
    if _has_connections(db, cable):
        raise EditError("Волокна кабеля сварены или заведены на кросс — сначала удалите сварки и порты")
    audit(db, user_id, "delete", "cable", cable.id, {"name": cable.name, "operator_id": cable.operator_id})
    db.delete(cable)
    db.flush()


def nodes_exist(db: Session, ids: list[int]) -> bool:
    return db.scalar(sa.select(sa.func.count(NetworkNode.id)).where(NetworkNode.id.in_(ids))) == len(set(ids))
