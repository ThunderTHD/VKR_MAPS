"""Анализ последствий и корреляция аварий.

Путь узла доступа — трасса света от порта кросса, на который заведён его линейный тракт
(access_node.cross_port_id), до противоположного порта (обычно — кросс узла оператора).
Через путь известны все участки трассы, кабели, муфты и колодцы, от которых зависит узел.

Отсюда два вопроса:
  * «что отключится», если повредить участок / кабель / муфту / колодец / вышестоящий
    коммутатор — прямой обход путей;
  * «где обрыв», если несколько узлов разом стали недоступны (корреляция) — элементы,
    общие для путей всех упавших узлов и не входящие в пути работающих. Для найденного
    участка сразу считается, на каком расстоянии его ждать на рефлектометре со стороны
    узла оператора.

Пути считаются трассировкой (этап 7) и кешируются; кеш сбрасывается, когда меняется
«отпечаток» сети — сварки, прокладка кабелей, подключения портов, тракты узлов доступа.
"""
from collections import Counter
from dataclasses import dataclass, field
from threading import Lock

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import AccessNode, NetworkNode
from app.services import tracing
from app.services.editing import EditError


@dataclass
class Span:
    """Участок трассы на пути узла: оптические расстояния от порта узла доступа."""

    segment_id: int
    cable_id: int
    cable: str
    fiber: int
    from_opt: float
    to_opt: float


@dataclass
class ServicePath:
    access_node_id: int
    hostname: str
    end_state: str                      # port | open | loop
    total_opt: float = 0.0
    far_port: str | None = None         # где путь заканчивается (порт кросса узла оператора)
    far_node_id: int | None = None
    near_node_id: int | None = None     # здание клиента (начало пути)
    spans: list[Span] = field(default_factory=list)
    segments: set[int] = field(default_factory=set)
    cables: set[int] = field(default_factory=set)
    closures: set[int] = field(default_factory=set)
    nodes: set[int] = field(default_factory=set)
    error: str | None = None


_cache: dict[str, tuple[str, dict[int, ServicePath]]] = {}
_lock = Lock()

FINGERPRINT_SQL = sa.text("""
    SELECT concat_ws(':',
        (SELECT count(*) || '/' || coalesce(sum(id), 0) FROM splice),
        (SELECT count(*) || '/' || coalesce(sum(segment_id::bigint * seq), 0) FROM cable_route),
        (SELECT coalesce(sum(coalesce(fiber_id, 0)::bigint * port_no), 0) FROM cross_port),
        (SELECT count(*) || '/' || coalesce(sum(coalesce(cross_port_id, 0)), 0) || '/'
                || coalesce(sum(coalesce(uplink_id, 0)), 0) FROM access_node),
        (SELECT max(updated_at)::text FROM cable),
        (SELECT max(updated_at)::text FROM route_segment))""")


def _build_path(db: Session, a: AccessNode) -> ServicePath:
    p = ServicePath(a.id, a.hostname or f"#{a.id}", "none")
    try:
        fiber, end, port = tracing.start_of_port(db, a.cross_port_id)
        tr = tracing.trace(db, fiber.id, end, start_port=port)
    except EditError as exc:
        p.error = str(exc)
        return p
    p.end_state, p.total_opt = tr.end_state, tr.total_opt
    last = tr.events[-1]
    if last.kind == "port":
        p.far_port, p.far_node_id = last.label, last.node_id
    p.near_node_id = tr.events[0].node_id
    for e in tr.elements:
        p.cables.add(e.cable_id)
        p.nodes.update(n for n in (e.node_id, e.to_node_id) if n is not None)
        if e.kind == "segment":
            p.segments.add(e.segment_id)
            p.spans.append(Span(e.segment_id, e.cable_id, e.cable, e.fiber, e.start_opt, e.start_opt + e.opt_len))
    p.closures = {ev.closure_id for ev in tr.events if ev.closure_id}
    return p


def service_paths(db: Session, *, refresh: bool = False) -> dict[int, ServicePath]:
    """Пути всех узлов доступа с заведённым трактом (из кеша, если сеть не менялась)."""
    bind = db.get_bind()
    key = str(getattr(bind, "url", None) or bind.engine.url)
    fp = db.execute(FINGERPRINT_SQL).scalar_one()
    with _lock:
        hit = _cache.get(key)
        if hit and hit[0] == fp and not refresh:
            return hit[1]
    nodes = db.scalars(sa.select(AccessNode).where(AccessNode.cross_port_id.is_not(None))
                       .order_by(AccessNode.id)).all()
    paths = {a.id: _build_path(db, a) for a in nodes}
    with _lock:
        _cache[key] = (fp, paths)
    return paths


def downstream(db: Session, access_node_id: int) -> set[int]:
    """Узлы, подключённые через данный коммутатор (по цепочке uplink, включая его самого)."""
    rows = db.execute(sa.text("""
        WITH RECURSIVE t(id) AS (
            SELECT CAST(:i AS int)
            UNION SELECT a.id FROM access_node a JOIN t ON a.uplink_id = t.id)
        SELECT id FROM t"""), {"i": access_node_id}).scalars().all()
    return set(rows)


# ------------------------------------------------------------------ что отключится

TARGETS = {"segment", "cable", "closure", "node", "access_node"}


def _hit(p: ServicePath, kind: str, obj_id: int) -> bool:
    return {"segment": p.segments, "cable": p.cables, "closure": p.closures, "node": p.nodes}[kind].__contains__(obj_id)


def _describe(db: Session, ids: set[int], paths: dict[int, ServicePath], reason: dict[int, str]) -> list[dict]:
    if not ids:
        return []
    rows = db.execute(sa.text("""
        SELECT a.id, a.hostname, host(a.ip) AS ip, a.status, a.address, o.short_name AS operator,
               n.id AS node_id, coalesce(n.name, n.code) AS node, ST_X(n.geom) AS x, ST_Y(n.geom) AS y
        FROM access_node a LEFT JOIN network_node n ON n.id = a.node_id
        LEFT JOIN operator o ON o.id = a.operator_id
        WHERE a.id = ANY(:ids) ORDER BY o.short_name NULLS LAST, a.hostname"""), {"ids": list(ids)}).mappings()
    out = []
    for r in rows:
        out.append({"id": r["id"], "hostname": r["hostname"], "ip": r["ip"], "status": str(r["status"]),
                    "address": r["address"], "operator": r["operator"], "node_id": r["node_id"], "node": r["node"],
                    "lonlat": [r["x"], r["y"]] if r["x"] is not None else None, "via": reason.get(r["id"], "")})
    return out


def impact(db: Session, kind: str, obj_id: int) -> dict:
    """Какие узлы доступа потеряют связь при повреждении объекта."""
    if kind not in TARGETS:
        raise EditError("Анализ последствий доступен для участка, кабеля, муфты, точки сети и узла доступа")
    paths = service_paths(db)
    hit: set[int] = set()
    reason: dict[int, str] = {}
    if kind == "access_node":
        if db.get(AccessNode, obj_id) is None:
            raise EditError("Узел доступа не найден")
        hit = downstream(db, obj_id) - {obj_id}
        reason = {i: "через вышестоящий коммутатор" for i in hit}
    else:
        for p in paths.values():
            if _hit(p, kind, obj_id):
                hit.add(p.access_node_id)
                reason[p.access_node_id] = "линейный тракт"
        # Повреждение колодца/здания узла оператора — пропадает и сам агрегирующий коммутатор с подчинёнными
        if kind == "node":
            for a_id in db.scalars(sa.select(AccessNode.id).where(AccessNode.node_id == obj_id)):
                for d in downstream(db, a_id):
                    if d not in hit:
                        hit.add(d)
                        reason[d] = "оборудование в этой точке" if d == a_id else "через вышестоящий коммутатор"
        # Подчинённые упавших коммутаторов тоже теряют связь
        for a_id in list(hit):
            for d in downstream(db, a_id) - hit:
                hit.add(d)
                reason[d] = "через вышестоящий коммутатор"
    items = _describe(db, hit, paths, reason)
    by_op = Counter(i["operator"] or "—" for i in items)
    untraced = sum(1 for p in paths.values() if p.error or p.end_state != "port")
    return {"kind": kind, "id": obj_id, "count": len(items), "access_nodes": items,
            "by_operator": [{"operator": k, "count": v} for k, v in by_op.most_common()],
            "paths_total": len(paths), "paths_incomplete": untraced}


# ------------------------------------------------------------------ корреляция аварий


def correlate(db: Session, down_ids: set[int] | None = None, *, limit: int = 5) -> dict:
    """Где вероятнее всего обрыв, если узлы down_ids недоступны (по умолчанию — текущие статусы).

    Кандидат — участок трассы. Счёт: сколько упавших узлов проходят через участок
    (все — лучше), минус работающие узлы через него (живой узел за участком почти
    исключает обрыв на нём). Если все упавшие подключены через один недоступный
    коммутатор — причина, скорее всего, в нём, а не в кабеле.
    """
    paths = service_paths(db)
    status = dict(db.execute(sa.select(AccessNode.id, AccessNode.status)).all())
    if down_ids is None:
        down_ids = {i for i, s in status.items() if str(s) == "down"}
    up_ids = {i for i, s in status.items() if str(s) == "up"} - down_ids
    result: dict = {"down": sorted(down_ids), "candidates": [], "uplink": None, "untraced": []}
    if not down_ids:
        return result

    # Общий вышестоящий коммутатор, который сам недоступен
    uplinks = dict(db.execute(sa.select(AccessNode.id, AccessNode.uplink_id)).all())
    for cand in {uplinks.get(i) for i in down_ids} - {None}:
        if cand in down_ids and downstream(db, cand) >= down_ids:
            a = db.get(AccessNode, cand)
            result["uplink"] = {"id": a.id, "hostname": a.hostname,
                                "message": f"Все недоступные узлы подключены через {a.hostname} — "
                                           "вероятна авария этого коммутатора или его питания"}
            break

    traced_down = [paths[i] for i in down_ids if i in paths and paths[i].segments]
    result["untraced"] = sorted(i for i in down_ids if i not in paths or not paths[i].segments)
    if not traced_down:
        return result
    seg_down = Counter(s for p in traced_down for s in p.segments)
    seg_up = Counter(s for i in up_ids if i in paths for s in paths[i].segments)
    n = len(traced_down)
    scored = []
    for seg, k in seg_down.items():
        alive = seg_up.get(seg, 0)
        scored.append((k / n - 0.5 * min(alive, 2), k, alive, seg))
    scored.sort(key=lambda t: (-t[0], t[3]))
    best_score = scored[0][0]
    # Участки с одинаковым лучшим счётом идут подряд по трассе — объединяем их в отрезок
    top = [t for t in scored if t[0] >= best_score - 1e-9]
    group = _group_spans(db, [t[3] for t in top], traced_down)
    if group:
        group.update({"down_through": top[0][1], "down_total": n, "alive_through": top[0][2],
                      "confidence": _confidence(top[0][1], n, top[0][2], len(top))})
        result["candidates"].append(group)
    # Следующие по счёту — как альтернативы
    shown = {s for g in result["candidates"] for s in g["segment_ids"]}
    for score, k, alive, seg in scored:
        if len(result["candidates"]) >= limit:
            break
        if seg in shown or score < 0.5:
            continue
        g = _group_spans(db, [seg], traced_down)
        if g:
            g.update({"down_through": k, "down_total": n, "alive_through": alive,
                      "confidence": _confidence(k, n, alive, 1)})
            result["candidates"].append(g)
            shown.add(seg)
    return result


def _confidence(k: int, n: int, alive: int, width: int) -> str:
    if alive:
        return "низкая"
    if k == n and n >= 2:
        return "высокая" if width <= 3 else "средняя"
    return "средняя" if k == n else "низкая"


def _group_spans(db: Session, seg_ids: list[int], down_paths: list[ServicePath]) -> dict | None:
    """Отрезок трассы из участков + ожидаемое расстояние на рефлектометре со стороны узла оператора."""
    if not seg_ids:
        return None
    ref = next((p for p in down_paths if set(seg_ids) & p.segments), None)
    rows = db.execute(sa.text("""
        SELECT s.id, coalesce(na.code, na.name) AS a, coalesce(nb.code, nb.name) AS b,
               ST_AsGeoJSON(s.geom, 7)::json AS gj
        FROM route_segment s JOIN network_node na ON na.id = s.node_a_id JOIN network_node nb ON nb.id = s.node_b_id
        WHERE s.id = ANY(:ids)"""), {"ids": seg_ids}).mappings().all()
    info = {r["id"]: r for r in rows}
    out = {"segment_ids": sorted(seg_ids),
           "segments": [{"id": i, "label": f"{info[i]['a']} – {info[i]['b']}"} for i in sorted(seg_ids) if i in info],
           "geojson": {"type": "FeatureCollection", "features": [
               {"type": "Feature", "geometry": info[i]["gj"], "properties": {"segment_id": i}}
               for i in seg_ids if i in info]}}
    if ref is not None:
        spans = [s for s in ref.spans if s.segment_id in set(seg_ids)]
        if spans and ref.end_state == "port":
            near = min(s.from_opt for s in spans)
            far = max(s.to_opt for s in spans)
            # Рефлектометр подключают на узле оператора (дальний конец пути): расстояние считается оттуда
            out["otdr"] = {"from_port": ref.far_port, "via_node": ref.hostname,
                           "min_m": round(ref.total_opt - far), "max_m": round(ref.total_opt - near),
                           "cables": sorted({f"{sp.cable}, ОВ {sp.fiber}" for sp in spans})}
    return out


def path_of(db: Session, access_node_id: int) -> dict:
    """Путь одного узла доступа — для подсветки на карте."""
    p = service_paths(db).get(access_node_id)
    if p is None:
        raise EditError("У узла доступа не указан порт кросса линейного тракта — путь неизвестен")
    if p.error:
        raise EditError(p.error)
    near = db.get(NetworkNode, p.near_node_id) if p.near_node_id else None
    feats = db.execute(sa.text("""
        SELECT json_build_object('type', 'Feature', 'geometry', ST_AsGeoJSON(geom, 7)::json,
                                 'properties', json_build_object('segment_id', id))
        FROM route_segment WHERE id = ANY(:ids)"""), {"ids": list(p.segments)}).scalars().all()
    return {"geojson": {"type": "FeatureCollection", "features": feats},
            "access_node_id": p.access_node_id, "end_state": p.end_state, "far_port": p.far_port,
            "total_opt_m": round(p.total_opt, 1), "segments": sorted(p.segments), "closures": sorted(p.closures),
            "cables": sorted(p.cables), "from": near.code or near.name if near else None}
