"""Рисование на карте: новые точки и участки трасс, изменение формы трассы.

Привязка (прилипание): если конец рисуемой линии ближе SNAP_M к существующей точке,
он считается этой точкой — так нарисованная трасса сразу соединяется с сетью. Интерфейс
прилипает к точкам по экранному расстоянию; сервер дополнительно проверяет по метрам,
чтобы не создать «почти совпадающую» точку-дубль.
"""
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import NetworkNode, NodeKind, RouteSegment, SegmentKind, SourceKind
from app.services.editing import NODE_FIELDS, EditError, _affected_cables, audit, snapshot
from app.services.network_ops import refresh_cable_geometry, refresh_segment_lengths

SNAP_M = 3.0


def _wkt_point(lon: float, lat: float) -> str:
    return f"SRID=4326;POINT({lon:.8f} {lat:.8f})"


def _wkt_line(coords: list[list[float]]) -> str:
    return "SRID=4326;LINESTRING(" + ", ".join(f"{x:.8f} {y:.8f}" for x, y in coords) + ")"


def nearest_node(db: Session, lon: float, lat: float, within_m: float) -> int | None:
    row = db.execute(sa.text("""
        SELECT id FROM network_node
        WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :d)
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) LIMIT 1"""),
        {"lon": lon, "lat": lat, "d": within_m}).first()
    return row[0] if row else None


def create_node(db: Session, *, lon: float, lat: float, kind: str = "well", code: str | None = None,
                name: str | None = None, owner_id: int | None = None, address: str | None = None,
                note: str | None = None, user_id: int | None = None, allow_near: bool = False) -> NetworkNode:
    if not allow_near and (other := nearest_node(db, lon, lat, SNAP_M)):
        raise EditError(f"Рядом (до {SNAP_M:g} м) уже есть точка id={other} — используйте её")
    node = NetworkNode(kind=NodeKind(kind), code=(code or "").strip() or None, name=(name or "").strip() or None,
                       owner_id=owner_id, address=address, note=note, geom=_wkt_point(lon, lat),
                       source_kind=SourceKind.manual)
    db.add(node)
    db.flush()
    audit(db, user_id, "create", "network_node", node.id, None, snapshot(node, NODE_FIELDS))
    return node


def _node_coords(db: Session, node_id: int) -> list[float]:
    row = db.execute(sa.text("SELECT ST_X(geom), ST_Y(geom) FROM network_node WHERE id = :id"), {"id": node_id}).first()
    if row is None:
        raise EditError(f"Точка id={node_id} не найдена")
    return [row[0], row[1]]


def _resolve_end(db: Session, end: dict, new_kind: str, owner_id: int | None, user_id: int | None) -> int:
    """Конец линии: {node_id} — существующая точка; {lon, lat} — прилипнуть или создать новую."""
    if end.get("node_id"):
        return int(end["node_id"])
    lon, lat = float(end["lon"]), float(end["lat"])
    if (near := nearest_node(db, lon, lat, SNAP_M)) is not None:
        return near
    return create_node(db, lon=lon, lat=lat, kind=end.get("kind") or new_kind,
                       owner_id=owner_id if new_kind == "well" else None, user_id=user_id, allow_near=True).id


def create_segment(db: Session, *, start: dict, end: dict, vertices: list[list[float]] | None = None,
                   kind: str = "duct", owner_id: int | None = None, name: str | None = None,
                   new_node_kind: str = "well", fact_length_m: float | None = None,
                   length_source: str | None = None, user_id: int | None = None) -> RouteSegment:
    """Нарисованный участок: концы — существующие точки или новые; промежуточные вершины — изгибы трассы."""
    a = _resolve_end(db, start, new_node_kind, owner_id, user_id)
    b = _resolve_end(db, end, new_node_kind, owner_id, user_id)
    if a == b:
        raise EditError("Начало и конец участка — одна и та же точка")
    if fact_length_m is not None and not length_source:
        raise EditError("Укажите источник фактической длины")
    coords = [_node_coords(db, a), *(vertices or []), _node_coords(db, b)]
    seg = RouteSegment(kind=SegmentKind(kind), owner_id=owner_id, node_a_id=a, node_b_id=b, geom=_wkt_line(coords),
                       geom_length_m=0, name=(name or "").strip() or None, fact_length_m=fact_length_m,
                       length_source=length_source, source_kind=SourceKind.manual)
    db.add(seg)
    db.flush()
    refresh_segment_lengths(db, [seg.id])
    db.refresh(seg)
    audit(db, user_id, "create", "route_segment", seg.id, None,
          {"kind": kind, "node_a_id": a, "node_b_id": b, "owner_id": owner_id, "length_m": round(seg.geom_length_m, 1)})
    return seg


def update_segment_geometry(db: Session, seg: RouteSegment, vertices: list[list[float]],
                            user_id: int | None) -> RouteSegment:
    """Новая форма трассы: промежуточные вершины меняются, концы всегда совпадают с точками."""
    before = db.execute(sa.text("SELECT ST_AsText(geom), geom_length_m FROM route_segment WHERE id = :id"),
                        {"id": seg.id}).one()
    coords = [_node_coords(db, seg.node_a_id), *vertices, _node_coords(db, seg.node_b_id)]
    db.execute(sa.text("UPDATE route_segment SET geom = ST_GeomFromEWKT(:g) WHERE id = :id"),
               {"g": _wkt_line(coords), "id": seg.id})
    refresh_segment_lengths(db, [seg.id])
    refresh_cable_geometry(db, _affected_cables(db, [seg.id]))
    db.refresh(seg)
    audit(db, user_id, "reshape", "route_segment", seg.id, {"wkt": before[0], "length_m": round(before[1], 1)},
          {"vertices": len(vertices), "length_m": round(seg.geom_length_m, 1)})
    return seg


def split_segment(db: Session, seg: RouteSegment, lon: float, lat: float, kind: str,
                  user_id: int | None) -> NetworkNode:
    """Вставить точку в участок (например, новый колодец на существующей трассе).

    Участок делится на два в ближайшей к точке позиции; кабели, проходившие по участку,
    будут проходить по обоим новым — порядок в маршруте сохраняется.
    """
    frac = db.execute(sa.text("""
        SELECT ST_LineLocatePoint(geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)) FROM route_segment
        WHERE id = :id"""), {"lon": lon, "lat": lat, "id": seg.id}).scalar_one()
    if frac <= 0.001 or frac >= 0.999:
        raise EditError("Точка совпадает с концом участка — разделять нечего")
    p = db.execute(sa.text("""
        SELECT ST_X(pt), ST_Y(pt) FROM (SELECT ST_LineInterpolatePoint(geom, :f) AS pt FROM route_segment
        WHERE id = :id) x"""), {"f": frac, "id": seg.id}).one()
    node = create_node(db, lon=p[0], lat=p[1], kind=kind, owner_id=seg.owner_id, user_id=user_id, allow_near=True)
    tail = db.execute(sa.text("SELECT ST_AsEWKT(ST_LineSubstring(geom, :f, 1)) FROM route_segment WHERE id = :id"),
                      {"f": frac, "id": seg.id}).scalar_one()
    # Вторая половина — новый участок; первая — исходный (сохраняет id, историю и длину по доле)
    second = RouteSegment(kind=seg.kind, owner_id=seg.owner_id, node_a_id=node.id, node_b_id=seg.node_b_id,
                          geom_length_m=0, name=seg.name, channels_total=seg.channels_total, geom=tail,
                          source_kind=SourceKind.manual)
    db.add(second)
    db.flush()
    db.execute(sa.text("UPDATE route_segment SET geom = ST_LineSubstring(geom, 0, :f), node_b_id = :n WHERE id = :id"),
               {"f": frac, "n": node.id, "id": seg.id})
    # Известная фактическая длина делится пропорционально (с тем же источником)
    if seg.fact_length_m is not None:
        total = seg.fact_length_m
        seg.fact_length_m = round(total * frac, 1)
        second.fact_length_m = round(total * (1 - frac), 1)
        second.length_source = seg.length_source
        second.length_note = f"разделено из участка id={seg.id}"
    db.flush()
    refresh_segment_lengths(db, [seg.id, second.id])
    # Маршруты кабелей: после участка seg вставляем second (с учётом направления прохода)
    rows = db.execute(sa.text("SELECT id, cable_id, seq, direction FROM cable_route WHERE segment_id = :id"),
                      {"id": seg.id}).all()
    for _, cable_id, seq, direction in rows:
        # Освобождаем место: сдвигаем последующие шаги (сначала в отрицательные, чтобы не нарушить уникальность)
        db.execute(sa.text("UPDATE cable_route SET seq = -seq - 1 WHERE cable_id = :c AND seq > :s"),
                   {"c": cable_id, "s": seq})
        db.execute(sa.text("UPDATE cable_route SET seq = -seq WHERE cable_id = :c AND seq < 0"), {"c": cable_id})
        if direction == 1:   # шли A→B: сначала seg (A→новая), потом second (новая→B)
            db.execute(sa.text("INSERT INTO cable_route (cable_id, seq, segment_id, direction) "
                               "VALUES (:c, :s, :seg, 1)"), {"c": cable_id, "s": seq + 1, "seg": second.id})
        else:                # шли B→A: сначала second (B→новая), потом seg (новая→A)
            db.execute(sa.text("UPDATE cable_route SET segment_id = :seg WHERE cable_id = :c AND seq = :s"),
                       {"seg": second.id, "c": cable_id, "s": seq})
            db.execute(sa.text("INSERT INTO cable_route (cable_id, seq, segment_id, direction) "
                               "VALUES (:c, :s, :seg, -1)"), {"c": cable_id, "s": seq + 1, "seg": seg.id})
    refresh_cable_geometry(db, [r[1] for r in rows])
    db.expire_all()
    audit(db, user_id, "split", "route_segment", seg.id, None, {"new_node_id": node.id, "new_segment_id": second.id})
    return node
