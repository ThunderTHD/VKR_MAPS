"""Карточки объектов (что показывается по клику на карте) и поиск."""
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    AccessNode,
    Cable,
    CableReserve,
    Closure,
    CrossPort,
    LengthSource,
    NetworkNode,
    OpticalCross,
    RouteSegment,
)

NODE_KIND_RU = {"well": "Колодец", "kks": "ККС", "pole": "Опора", "site": "Узел связи", "building": "Здание",
                "closure_point": "Точка муфты", "unknown": "Неизвестная точка"}
SEGMENT_KIND_RU = {"duct": "Кабельная канализация", "microduct": "Микроканализация", "aerial": "Подвес по опорам",
                   "facade": "По фасаду", "indoor": "Внутри здания", "hdd": "Прокол ГНБ", "ground": "В грунте"}
STATUS_RU = {"up": "Доступен", "down": "Недоступен", "unknown": "Нет данных"}


def _ref(kind: str, id_: int, label: str) -> dict:
    """Ссылка на другой объект — в интерфейсе по ней можно перейти."""
    return {"type": kind, "id": id_, "label": label}


def _node_label(n: NetworkNode | None) -> str:
    if n is None:
        return "—"
    return " · ".join(x for x in (n.code, n.name) if x) or f"{NODE_KIND_RU[n.kind.value]} #{n.id}"


def _lonlat(db: Session, geom) -> list[float]:
    x, y = db.execute(sa.select(sa.func.ST_X(geom), sa.func.ST_Y(geom))).one()
    return [x, y]


def node_card(db: Session, node_id: int) -> dict | None:
    n = db.get(NetworkNode, node_id)
    if n is None:
        return None
    segments = db.scalars(sa.select(RouteSegment).where(
        sa.or_(RouteSegment.node_a_id == n.id, RouteSegment.node_b_id == n.id))).all()
    seg_ids = [s.id for s in segments]
    cables = db.execute(sa.text("""
        SELECT DISTINCT c.id, c.name, o.short_name FROM cable_route cr
        JOIN cable c ON c.id = cr.cable_id JOIN operator o ON o.id = c.operator_id
        WHERE cr.segment_id = ANY(:ids) ORDER BY c.name"""), {"ids": seg_ids}).all()
    reserves = db.execute(sa.select(CableReserve, Cable.name).join(Cable).where(CableReserve.node_id == n.id)).all()
    return {
        "type": "node", "id": n.id, "title": _node_label(n), "subtitle": NODE_KIND_RU[n.kind.value],
        "lonlat": _lonlat(db, n.geom),
        "fields": [
            ["Тип", NODE_KIND_RU[n.kind.value]], ["Код", n.code], ["Название", n.name],
            ["Владелец", n.owner.short_name if n.owner else None], ["Адрес", n.address], ["Примечание", n.note],
        ],
        "sections": [
            {"title": "Муфты", "items": [
                _ref("closure", c.id, c.number)
                for c in db.scalars(sa.select(Closure).where(Closure.node_id == n.id))]},
            {"title": "Кроссы", "items": [
                _ref("cross", c.id, f"{c.name} ({c.port_count} портов)")
                for c in db.scalars(sa.select(OpticalCross).where(OpticalCross.node_id == n.id))]},
            {"title": "Узлы доступа", "items": [
                _ref("access_node", a.id, a.hostname or f"#{a.id}")
                for a in db.scalars(sa.select(AccessNode).where(AccessNode.node_id == n.id))]},
            {"title": "Участки трасс", "items": [
                _ref("segment", s.id, f"{SEGMENT_KIND_RU[s.kind.value]}, {s.best_length_m:.0f} м") for s in segments]},
            {"title": "Кабели через узел", "items": [_ref("cable", cid, f"{name} ({op})") for cid, name, op in cables]},
            {"title": "Запасы кабеля", "items": [
                _ref("cable", r.cable_id, f"{name}: {r.length_m:.0f} м") for r, name in reserves]},
        ],
    }


def segment_card(db: Session, seg_id: int) -> dict | None:
    s = db.get(RouteSegment, seg_id)
    if s is None:
        return None
    src = db.get(LengthSource, s.length_source) if s.length_source else None
    cables = db.execute(sa.text("""
        SELECT c.id, c.name, o.short_name, cr.channel_no FROM cable_route cr
        JOIN cable c ON c.id = cr.cable_id JOIN operator o ON o.id = c.operator_id
        WHERE cr.segment_id = :id ORDER BY o.short_name, c.name"""), {"id": s.id}).all()
    fact = f"{s.fact_length_m:.1f} м ({src.name}, ±{src.default_error_pct:g} %)" if s.fact_length_m else "не известна"
    return {
        "type": "segment", "id": s.id, "title": s.name or f"{_node_label(s.node_a)} — {_node_label(s.node_b)}",
        "subtitle": SEGMENT_KIND_RU[s.kind.value],
        "fields": [
            ["Тип", SEGMENT_KIND_RU[s.kind.value]], ["Владелец", s.owner.short_name if s.owner else None],
            ["Длина по карте", f"{s.geom_length_m:.1f} м"], ["Фактическая длина", fact],
            ["Каналов", s.channels_total],
        ],
        "sections": [
            {"title": "Концы", "items": [_ref("node", s.node_a_id, _node_label(s.node_a)),
                                         _ref("node", s.node_b_id, _node_label(s.node_b))]},
            {"title": f"Кабели ({len(cables)})", "items": [
                _ref("cable", cid, f"{name} — {op}" + (f", канал {ch}" if ch else ""))
                for cid, name, op, ch in cables]},
        ],
    }


def cable_card(db: Session, cable_id: int) -> dict | None:
    c = db.get(Cable, cable_id)
    if c is None:
        return None
    phys = sum(r.segment.best_length_m for r in c.route)
    reserves = sum(r.length_m for r in c.reserves)
    closures = db.execute(sa.text("""
        SELECT DISTINCT cl.id, cl.number FROM closure cl
        JOIN splice sp ON sp.closure_id = cl.id JOIN fiber f ON f.id IN (sp.fiber_a_id, sp.fiber_b_id)
        WHERE f.cable_id = :id ORDER BY cl.number"""), {"id": c.id}).all()
    known = sum(1 for r in c.route if r.segment.fact_length_m is not None)
    return {
        "type": "cable", "id": c.id, "title": c.name, "subtitle": f"{c.operator.short_name} · {c.cable_type.mark}",
        "fields": [
            ["Оператор", c.operator.short_name], ["Марка", c.cable_type.mark],
            ["Волокон", f"{c.cable_type.fiber_count} ({c.cable_type.modules}×{c.cable_type.fibers_per_module})"],
            ["Длина по трассе", f"{phys:.0f} м"], ["Запасы", f"{reserves:.0f} м"],
            ["Физическая длина", f"{phys + reserves:.0f} м"],
            ["Коэффициент укрутки", f"{c.twist_factor * 100:.2f} %"],
            ["Участков с известной длиной", f"{known} из {len(c.route)}"], ["Год прокладки", c.lay_year],
        ],
        "sections": [
            {"title": "Начало и конец", "items": [_ref("node", c.start_node_id, _node_label(c.start_node)),
                                                  _ref("node", c.end_node_id, _node_label(c.end_node))]},
            {"title": "Муфты на кабеле", "items": [_ref("closure", i, num) for i, num in closures]},
        ],
    }


def closure_card(db: Session, closure_id: int) -> dict | None:
    cl = db.get(Closure, closure_id)
    if cl is None:
        return None
    cable_refs = db.execute(sa.text("""
        SELECT DISTINCT c.id, c.name FROM splice sp
        JOIN fiber f ON f.id IN (sp.fiber_a_id, sp.fiber_b_id) JOIN cable c ON c.id = f.cable_id
        WHERE sp.closure_id = :id ORDER BY c.name"""), {"id": cl.id}).all()
    rows = db.execute(sa.text("""
        SELECT ca.name, fa.number, fa.color, cb.name, fb.number, fb.color, sp.note, sp.loss_splicer_db
        FROM splice sp
        JOIN fiber fa ON fa.id = sp.fiber_a_id JOIN cable ca ON ca.id = fa.cable_id
        JOIN fiber fb ON fb.id = sp.fiber_b_id JOIN cable cb ON cb.id = fb.cable_id
        WHERE sp.closure_id = :id ORDER BY ca.name, fa.number"""), {"id": cl.id}).all()
    cables = sorted({r[0] for r in rows} | {r[3] for r in rows})
    return {
        "type": "closure", "id": cl.id, "title": f"Муфта {cl.number}", "subtitle": _node_label(cl.node),
        "lonlat": _lonlat(db, cl.node.geom),
        "fields": [["Номер", cl.number], ["Модель", cl.model],
                   ["Оператор", cl.operator.short_name if cl.operator else None],
                   ["Сварок", len(rows)], ["Кабелей", len(cables)], ["Примечание", cl.note]],
        "sections": [{"title": "Узел", "items": [_ref("node", cl.node_id, _node_label(cl.node))]},
                     {"title": "Кабели в муфте", "items": [_ref("cable", i, n) for i, n in cable_refs]}],
        "table": {
            "title": "Рассветка",
            "columns": ["Кабель A", "ОВ", "Цвет", "Кабель Б", "ОВ", "Цвет", "Потери, дБ", "Примечание"],
            "rows": [[a, na, ca, b, nb, cb, loss, note] for a, na, ca, b, nb, cb, note, loss in rows],
        },
    }


def cross_card(db: Session, cross_id: int) -> dict | None:
    x = db.get(OpticalCross, cross_id)
    if x is None:
        return None
    rows = db.execute(sa.select(CrossPort.port_no, Cable.name, sa.text("fiber.number"), sa.text("fiber.color"),
                                CrossPort.note)
                      .select_from(CrossPort).outerjoin(CrossPort.fiber).outerjoin(Cable)
                      .where(CrossPort.cross_id == x.id, CrossPort.fiber_id.is_not(None))
                      .order_by(CrossPort.port_no)).all()
    return {
        "type": "cross", "id": x.id, "title": x.name, "subtitle": _node_label(x.node),
        "lonlat": _lonlat(db, x.node.geom),
        "fields": [["Модель", x.model], ["Портов", x.port_count], ["Занято", len(rows)], ["Разъём", x.connector]],
        "sections": [{"title": "Узел", "items": [_ref("node", x.node_id, _node_label(x.node))]}],
        "table": {"title": "Порты", "columns": ["Порт", "Кабель", "ОВ", "Цвет", "Примечание"],
                  "rows": [list(r) for r in rows]},
    }


def access_node_card(db: Session, an_id: int) -> dict | None:
    a = db.get(AccessNode, an_id)
    if a is None:
        return None
    return {
        "type": "access_node", "id": a.id, "title": a.hostname or f"Узел доступа #{a.id}",
        "subtitle": a.address or _node_label(a.node), "lonlat": _lonlat(db, a.node.geom) if a.node else None,
        "status": a.status.value,
        "fields": [["Состояние", STATUS_RU[a.status.value]], ["IP-адрес", str(a.ip) if a.ip else None],
                   ["Модель", a.model], ["Серийный номер", a.serial], ["Адрес", a.address],
                   ["Uplink", f"{a.uplink.hostname} {a.uplink_port or ''}".strip() if a.uplink else None]],
        "sections": [
            {"title": "Узел", "items": [_ref("node", a.node_id, _node_label(a.node))] if a.node else []},
            {"title": "Вышестоящий узел", "items": [_ref("access_node", a.uplink_id, a.uplink.hostname)]
             if a.uplink else []},
        ],
    }


CARDS = {"node": node_card, "segment": segment_card, "cable": cable_card, "closure": closure_card,
         "cross": cross_card, "access_node": access_node_card}


SEARCH_SQL = r"""
SELECT * FROM (
    SELECT 'node' AS type, n.id, concat_ws(' · ', n.code, n.name, n.address) AS label, n.kind AS hint,
           ST_X(n.geom) AS lon, ST_Y(n.geom) AS lat
    FROM network_node n
    WHERE n.code ILIKE :q OR regexp_replace(n.code, '[№.\s]', '', 'g') ILIKE :qn
       OR n.name ILIKE :q OR n.address ILIKE :q
    UNION ALL
    SELECT 'closure', cl.id, 'Муфта ' || cl.number || coalesce(' · ' || n.code, ''), 'closure',
           ST_X(n.geom), ST_Y(n.geom)
    FROM closure cl JOIN network_node n ON n.id = cl.node_id
    WHERE cl.number ILIKE :q OR regexp_replace(cl.number, '[№.\s]', '', 'g') ILIKE :qn
    UNION ALL
    SELECT 'cable', c.id, c.name, 'cable', ST_X(ST_Centroid(c.geom)), ST_Y(ST_Centroid(c.geom))
    FROM cable c WHERE c.name ILIKE :q
    UNION ALL
    SELECT 'access_node', a.id, concat_ws(' · ', a.hostname, host(a.ip), a.address), 'access_node',
           ST_X(n.geom), ST_Y(n.geom)
    FROM access_node a JOIN network_node n ON n.id = a.node_id
    WHERE a.hostname ILIKE :q OR host(a.ip) ILIKE :q OR a.address ILIKE :q
) r ORDER BY length(label), label LIMIT :limit
"""


def search(db: Session, query: str, limit: int = 20) -> list[dict]:
    """Поиск по коду/названию/адресу узла, номеру муфты, кабелю, hostname и IP.

    «№» и точки в коде колодца необязательны: «к348», «к.348» и «к.№348» найдут одно и то же.
    """
    q = query.strip()
    if len(q) < 2:
        return []
    normalized = "".join(ch for ch in q if ch not in "№." and not ch.isspace())
    params = {"q": f"%{q}%", "qn": f"%{normalized}%", "limit": limit}
    rows = db.execute(sa.text(SEARCH_SQL), params).mappings().all()
    return [dict(r) for r in rows]

