"""Трассировка волокна: путь света от порта кросса (или конца волокна) по сваркам.

Свет входит в волокно с одного конца и выходит с другого. На выходном конце он либо
продолжает путь по сварке в следующее волокно, либо попадает в порт кросса (конец
тракта), либо конец ничем не занят (обрыв описания трассы). Так получается
последовательность «ног» (legs) — отрезков пути по одному кабелю.

Для каждой ноги строится профиль: запасы и участки трасс в порядке прохождения с
физической длиной, её погрешностью и геометрией в направлении движения света.
Оптическая длина элемента = физическая × (1 + коэффициент укрутки кабеля), потому что
волокно в модуле уложено с избытком относительно оболочки кабеля.
Профиль — основа локализации (localization.py) и списка ожидаемых событий рефлектограммы.
"""
from dataclasses import dataclass, field

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Cable, CrossPort, Fiber, FiberEnd, LengthSourceCode, Splice
from app.services.calib_state import TWIST_ERR_2S, CalibState, current_state
from app.services.editing import EditError

MAX_LEGS = 200  # защита от зацикливания на ошибочной рассветке


@dataclass
class Element:
    """Элемент профиля: участок трассы или запас кабеля в узле."""

    kind: str                     # "segment" | "reserve"
    cable_id: int
    cable: str
    fiber: int
    twist: float
    length_m: float               # физическая длина (лучшая известная)
    err2_m: float                 # погрешность длины, 2σ (≈95 %), м
    source: str                   # источник длины
    segment_id: int | None = None
    reserve_id: int | None = None
    coords: list[list[float]] = field(default_factory=list)  # геометрия в направлении света
    node_id: int | None = None    # для запаса — узел; для участка — узел в начале
    to_node_id: int | None = None
    twist_err: float = TWIST_ERR_2S   # погрешность укрутки (2σ)
    raw_geom_m: float | None = None   # для участка «по карте» — длина нарисованной линии без поправки
    start_phys: float = 0.0       # физическое расстояние от начала тракта до элемента
    start_opt: float = 0.0        # оптическое

    @property
    def opt_len(self) -> float:
        return self.length_m * (1 + self.twist)


@dataclass
class Event:
    """Ожидаемое событие на рефлектограмме: сварка в муфте, порт кросса, конец описания."""

    kind: str                     # "start" | "splice" | "port" | "open"
    opt_m: float = 0.0
    phys_m: float = 0.0
    node_id: int | None = None
    label: str = ""
    closure_id: int | None = None
    cross_id: int | None = None
    splice_id: int | None = None
    loss_db: float | None = None


@dataclass
class Leg:
    cable_id: int
    cable: str
    fiber_id: int
    fiber: int
    color: str | None
    entry: FiberEnd
    twist: float


@dataclass
class Trace:
    legs: list[Leg] = field(default_factory=list)
    elements: list[Element] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    end_state: str = "open"       # "port" | "open" | "loop"
    node_labels: dict[int, str] = field(default_factory=dict)
    node_points: dict[int, list[float]] = field(default_factory=dict)

    @property
    def total_phys(self) -> float:
        return sum(e.length_m for e in self.elements)

    @property
    def total_opt(self) -> float:
        return sum(e.opt_len for e in self.elements)


def _err_pct(db: Session) -> dict[str, float]:
    return {str(code.value if hasattr(code, "value") else code): pct
            for code, pct in db.execute(sa.text("SELECT code, default_error_pct FROM length_source"))}


def start_of_port(db: Session, port_id: int) -> tuple[Fiber, FiberEnd, CrossPort]:
    port = db.get(CrossPort, port_id)
    if port is None:
        raise EditError("Порт не найден")
    if port.fiber_id is None:
        raise EditError(f"Порт {port.port_no} кросса «{port.cross.name}» свободен — к нему не подключено волокно")
    return port.fiber, port.fiber_end, port


def trace(db: Session, fiber_id: int, entry: str | FiberEnd, *, start_port: CrossPort | None = None,
          state: CalibState | None = None) -> Trace:
    """Пройти путь света, начиная с конца `entry` волокна `fiber_id`.

    state — параметры калибровки (по умолчанию действующие); калибровка сама строит
    трассы с «сырыми» параметрами (calib_state.RAW), чтобы оценивать их заново.
    """
    state = state if state is not None else current_state(db)
    fiber = db.get(Fiber, fiber_id)
    if fiber is None:
        raise EditError("Волокно не найдено")
    entry = FiberEnd(entry)
    err = _err_pct(db)
    tr = Trace()
    first_node = fiber.cable.start_node_id if entry == FiberEnd.a else fiber.cable.end_node_id
    start_label = (f"{start_port.cross.name}, порт {start_port.port_no}" if start_port
                   else f"{fiber.cable.name}, ОВ {fiber.number}")
    tr.events.append(Event("start", node_id=first_node, label=start_label,
                           cross_id=start_port.cross_id if start_port else None))
    seen: set[tuple[int, str]] = set()
    phys = opt = 0.0
    while True:
        if (fiber.id, entry.value) in seen or len(tr.legs) >= MAX_LEGS:
            tr.end_state = "loop"
            tr.events.append(Event("open", opt, phys, label="Петля в рассветке — путь зациклился"))
            break
        seen.add((fiber.id, entry.value))
        cable = fiber.cable
        tr.legs.append(Leg(cable.id, cable.name, fiber.id, fiber.number, fiber.color, entry, cable.twist_factor))
        for el in _leg_elements(db, cable, fiber, entry, err, state):
            el.start_phys, el.start_opt = phys, opt
            phys += el.length_m
            opt += el.opt_len
            tr.elements.append(el)
        exit_end = FiberEnd.b if entry == FiberEnd.a else FiberEnd.a
        exit_node = cable.end_node_id if entry == FiberEnd.a else cable.start_node_id
        sp = db.scalars(sa.select(Splice).where(sa.or_(
            sa.and_(Splice.fiber_a_id == fiber.id, Splice.end_a == exit_end),
            sa.and_(Splice.fiber_b_id == fiber.id, Splice.end_b == exit_end)))).first()
        if sp is not None:
            nxt, nxt_end = ((sp.fiber_b, sp.end_b) if sp.fiber_a_id == fiber.id and sp.end_a == exit_end
                            else (sp.fiber_a, sp.end_a))
            where = f"муфта {sp.closure.number}" if sp.closure else "кросс"
            tr.events.append(Event("splice", opt, phys, exit_node,
                                   f"{where}: {cable.name} ОВ {fiber.number} → {nxt.cable.name} ОВ {nxt.number}",
                                   closure_id=sp.closure_id, splice_id=sp.id, loss_db=sp.loss_splicer_db))
            fiber, entry = nxt, FiberEnd(nxt_end)
            continue
        port = db.scalars(sa.select(CrossPort).where(CrossPort.fiber_id == fiber.id,
                                                      CrossPort.fiber_end == exit_end)).first()
        if port is not None:
            tr.end_state = "port"
            tr.events.append(Event("port", opt, phys, exit_node, f"{port.cross.name}, порт {port.port_no}",
                                   cross_id=port.cross_id))
        else:
            tr.end_state = "open"
            tr.events.append(Event("open", opt, phys, exit_node,
                                   f"{cable.name} ОВ {fiber.number}: конец волокна никуда не подключён"))
        break
    _node_info(db, tr)
    return tr


def _leg_elements(db: Session, cable: Cable, fiber: Fiber, entry: FiberEnd, err: dict,
                  state: CalibState) -> list[Element]:
    """Запасы и участки кабеля в порядке движения света."""
    rows = db.execute(sa.text("""
        SELECT cr.seq, s.id, cr.direction, s.node_a_id, s.node_b_id, s.fact_length_m, s.length_source,
               s.geom_length_m,
               ST_AsGeoJSON(CASE WHEN cr.direction = 1 THEN s.geom ELSE ST_Reverse(s.geom) END, 7)::json
        FROM cable_route cr JOIN route_segment s ON s.id = cr.segment_id
        WHERE cr.cable_id = :c ORDER BY cr.seq"""), {"c": cable.id}).all()
    reserves = {r.node_id: r for r in cable.reserves}
    tw = cable.twist_factor
    tw_err = cable.twist_err_2s if cable.twist_err_2s is not None else TWIST_ERR_2S
    geo_pct = state.geo_err_pct if state.geo_err_pct is not None else err.get("geometry", 25.0)
    steps = []
    for _, seg_id, direction, na, nb, fact, src, geom_len, gj in rows:
        frm, to = (na, nb) if direction == 1 else (nb, na)
        coords = gj["coordinates"]
        raw = None
        if fact is not None:
            length, source = fact, str(src.value if hasattr(src, "value") else src)
            err2 = length * err.get(source, 25.0) / 100
        else:
            # Длина только по карте: с поправкой калибровки «реальная трасса длиннее нарисованной»
            raw, source = geom_len, LengthSourceCode.geometry.value
            length = geom_len * (1 + state.map_stretch)
            err2 = length * geo_pct / 100
        steps.append(Element("segment", cable.id, cable.name, fiber.number, tw, length, err2, source,
                             segment_id=seg_id, coords=coords, node_id=frm, to_node_id=to, twist_err=tw_err,
                             raw_geom_m=raw))
    if entry == FiberEnd.b:  # свет идёт от конца кабеля к началу
        for s in steps:
            s.coords = list(reversed(s.coords))
            s.node_id, s.to_node_id = s.to_node_id, s.node_id
        steps.reverse()
    nodes = [steps[0].node_id] + [s.to_node_id for s in steps] if steps else []
    result: list[Element] = []
    for i, node in enumerate(nodes):
        r = reserves.get(node)
        if r is not None:
            src = str(r.length_source.value if hasattr(r.length_source, "value") else r.length_source)
            result.append(Element("reserve", cable.id, cable.name, fiber.number, tw, r.length_m,
                                  r.length_m * err.get(src, 10.0) / 100, src, reserve_id=r.id, node_id=node,
                                  to_node_id=node, twist_err=tw_err))
        if i < len(steps):
            result.append(steps[i])
    return result


def _node_info(db: Session, tr: Trace) -> None:
    ids = {e.node_id for e in tr.elements} | {e.to_node_id for e in tr.elements} | {e.node_id for e in tr.events}
    ids.discard(None)
    if not ids:
        return
    for nid, code, name, x, y in db.execute(sa.text("""
            SELECT id, code, name, ST_X(geom), ST_Y(geom) FROM network_node WHERE id = ANY(:ids)"""),
            {"ids": list(ids)}):
        tr.node_labels[nid] = code or name or f"точка #{nid}"
        tr.node_points[nid] = [x, y]


# ------------------------------------------------------------------ представление для API


def path_geojson(tr: Trace) -> dict:
    feats = []
    for e in tr.elements:
        if e.kind == "segment":
            feats.append({"type": "Feature", "geometry": {"type": "LineString", "coordinates": e.coords},
                          "properties": {"segment_id": e.segment_id, "cable": e.cable, "fiber": e.fiber,
                                         "from_m": round(e.start_opt, 1), "to_m": round(e.start_opt + e.opt_len, 1)}})
    for ev in tr.events:
        if ev.node_id in tr.node_points:
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": tr.node_points[ev.node_id]},
                          "properties": {"event": ev.kind, "label": ev.label, "opt_m": round(ev.opt_m, 1)}})
    return {"type": "FeatureCollection", "features": feats}


def trace_summary(tr: Trace) -> dict:
    """Ответ API трассировки: ноги, ожидаемые события, итог."""
    return {
        "end_state": tr.end_state,
        "total_phys_m": round(tr.total_phys, 1),
        "total_opt_m": round(tr.total_opt, 1),
        "legs": [{"cable_id": lg.cable_id, "cable": lg.cable, "fiber_id": lg.fiber_id, "fiber": lg.fiber,
                  "color": lg.color, "entry": lg.entry.value, "twist": lg.twist} for lg in tr.legs],
        "events": [{"kind": ev.kind, "opt_m": round(ev.opt_m, 1), "phys_m": round(ev.phys_m, 1),
                    "label": ev.label, "node": tr.node_labels.get(ev.node_id), "node_id": ev.node_id,
                    "closure_id": ev.closure_id, "cross_id": ev.cross_id, "loss_db": ev.loss_db}
                   for ev in tr.events],
        "geojson": path_geojson(tr),
    }
