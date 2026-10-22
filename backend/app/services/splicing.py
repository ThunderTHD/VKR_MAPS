"""Муфты, сварки (рассветка), оптические кроссы, разрезание кабеля в муфте.

Модель: у каждого волокна два конца — `a` (у начала кабеля) и `b` (у конца). В муфте,
установленной в узле, доступны концы кабелей, которые в этом узле начинаются (конец `a`)
или заканчиваются (конец `b`). Сварка соединяет два таких конца; один конец может быть
занят только одной сваркой или одним портом кросса — это проверяет триггер базы
(миграция 0003), а здесь ошибка превращается в понятное сообщение.

Кабель, который проходит через узел, не прерываясь (транзит), сварить нельзя: сначала
его нужно «разрезать» в муфте — он делится на два кабеля, а все волокна сразу
соединяются транзитными сварками. После этого отдельные транзитные сварки можно
заменить ответвлениями — так в модели выглядит «вскрытие модуля» в муфте.
"""
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    Cable,
    CableReserve,
    Closure,
    CrossPort,
    Fiber,
    FiberEnd,
    NetworkNode,
    OpticalCross,
    SourceKind,
    Splice,
)
from app.services.editing import EditError, audit
from app.services.network_ops import make_fibers, refresh_cable_geometry

# Потери на сварке выше этого значения выделяются в проверке муфты. Типичная сварка
# современным аппаратом — 0,02–0,05 дБ; 0,2 дБ — консервативная граница «надо переварить».
LOSS_WARN_DB = 0.2
TRANSIT_NOTE = "транзит"


def _db_error(exc: sa.exc.DBAPIError) -> EditError:
    """Сообщение триггера или ограничения базы — пользователю, без технических подробностей."""
    diag = getattr(exc.orig, "diag", None)
    text = getattr(diag, "message_primary", None) or str(exc.orig).splitlines()[0]
    return EditError(text)


def _flush(db: Session, *new: object) -> None:
    """Записать изменения в точке сохранения: ошибка базы откатывает только их, а не всю транзакцию.

    Новые объекты добавляются внутри точки сохранения: begin_nested() сам сбрасывает в базу
    ожидающие изменения, и если сделать db.add() до него, ошибка случится вне точки сохранения.
    """
    try:
        with db.begin_nested():
            db.add_all(new)
            db.flush()
    except sa.exc.DBAPIError as exc:
        raise _db_error(exc) from None


# ------------------------------------------------------------------ концы кабелей в узле


@dataclass
class CableEnd:
    cable: Cable
    end: FiberEnd  # какой конец волокон этого кабеля находится в узле


def cable_ends_at(db: Session, node_id: int) -> list[CableEnd]:
    """Кабели, которые начинаются или заканчиваются в узле (кабель-петля — дважды)."""
    result = []
    for c in db.scalars(sa.select(Cable).where(sa.or_(Cable.start_node_id == node_id, Cable.end_node_id == node_id))
                        .order_by(Cable.name, Cable.id)):
        if c.start_node_id == node_id:
            result.append(CableEnd(c, FiberEnd.a))
        if c.end_node_id == node_id:
            result.append(CableEnd(c, FiberEnd.b))
    return result


def transit_cables(db: Session, node_id: int) -> list[Cable]:
    """Кабели, проходящие через узел без разреза (узел — внутренняя точка маршрута)."""
    ids = db.scalars(sa.text("""
        WITH steps AS (
            SELECT cr.cable_id, cr.seq,
                   CASE WHEN cr.direction = 1 THEN s.node_b_id ELSE s.node_a_id END AS to_node,
                   max(cr.seq) OVER (PARTITION BY cr.cable_id) AS last_seq
            FROM cable_route cr JOIN route_segment s ON s.id = cr.segment_id)
        SELECT DISTINCT cable_id FROM steps WHERE to_node = :n AND seq < last_seq""").bindparams(n=node_id)).all()
    return list(db.scalars(sa.select(Cable).where(Cable.id.in_(ids)).order_by(Cable.name))) if ids else []


def _end_at(db: Session, fiber: Fiber, node_id: int, end: str | None) -> FiberEnd:
    """Какой конец волокна находится в узле; если оба (кабель-петля) — нужен явный `end`."""
    c = fiber.cable
    ends = [e for e, n in ((FiberEnd.a, c.start_node_id), (FiberEnd.b, c.end_node_id)) if n == node_id]
    if not ends:
        raise EditError(f"Кабель «{c.name}» не начинается и не заканчивается в этом узле — "
                        "если он проходит транзитом, сначала разрежьте его в муфте")
    if end:
        if FiberEnd(end) not in ends:
            raise EditError(f"Конец {end} кабеля «{c.name}» находится в другом узле")
        return FiberEnd(end)
    if len(ends) > 1:
        raise EditError(f"Кабель «{c.name}» начинается и заканчивается в этом узле — укажите конец (a или b)")
    return ends[0]


# ------------------------------------------------------------------ муфты

CLOSURE_FIELDS = ["number", "model", "operator_id", "note"]


def create_closure(db: Session, *, node_id: int, number: str, model: str | None = None,
                   operator_id: int | None = None, note: str | None = None, user_id: int | None = None) -> Closure:
    if db.get(NetworkNode, node_id) is None:
        raise EditError("Узел не найден")
    if not number.strip():
        raise EditError("Укажите номер муфты")
    clo = Closure(node_id=node_id, number=number.strip(), model=model, operator_id=operator_id, note=note,
                  source_kind=SourceKind.manual)
    db.add(clo)
    db.flush()
    audit(db, user_id, "create", "closure", clo.id, None, {f: getattr(clo, f) for f in CLOSURE_FIELDS})
    return clo


def update_closure(db: Session, clo: Closure, changes: dict, user_id: int | None) -> Closure:
    before = {f: getattr(clo, f) for f in CLOSURE_FIELDS}
    for k, v in changes.items():
        if k not in CLOSURE_FIELDS:
            raise EditError(f"Поле «{k}» менять нельзя")
        setattr(clo, k, v.strip() if isinstance(v, str) else v)
    if not clo.number:
        raise EditError("Укажите номер муфты")
    db.flush()
    audit(db, user_id, "update", "closure", clo.id, before, {f: getattr(clo, f) for f in CLOSURE_FIELDS})
    return clo


def delete_closure(db: Session, clo: Closure, user_id: int | None) -> int:
    """Удалить муфту вместе с её сварками. Возвращает число удалённых сварок."""
    n = len(clo.splices)
    audit(db, user_id, "delete", "closure", clo.id, {"number": clo.number, "node_id": clo.node_id, "splices": n})
    db.delete(clo)
    db.flush()
    return n


# ------------------------------------------------------------------ сварки


def add_splice(db: Session, clo: Closure, fiber_a_id: int, fiber_b_id: int, *, end_a: str | None = None,
               end_b: str | None = None, loss_db: float | None = None, note: str | None = None,
               user_id: int | None = None) -> Splice:
    fa, fb = db.get(Fiber, fiber_a_id), db.get(Fiber, fiber_b_id)
    if fa is None or fb is None:
        raise EditError("Волокно не найдено")
    ea, eb = _end_at(db, fa, clo.node_id, end_a), _end_at(db, fb, clo.node_id, end_b)
    if fa.id == fb.id:
        raise EditError("Нельзя сварить волокно само с собой")
    if loss_db is not None and not 0 <= loss_db < 5:
        raise EditError("Потери на сварке — от 0 до 5 дБ")
    sp = Splice(closure_id=clo.id, fiber_a_id=fa.id, end_a=ea, fiber_b_id=fb.id, end_b=eb,
                loss_splicer_db=loss_db, note=(note or "").strip() or None)
    _flush(db, sp)
    audit(db, user_id, "splice", "closure", clo.id, None,
          {"splice_id": sp.id, "a": f"{fa.cable.name} ОВ {fa.number}", "b": f"{fb.cable.name} ОВ {fb.number}"})
    return sp


def splice_range(db: Session, clo: Closure, *, cable_a_id: int, from_a: int, cable_b_id: int, from_b: int,
                 count: int, end_a: str | None = None, end_b: str | None = None, note: str | None = None,
                 user_id: int | None = None) -> int:
    """Сварить подряд: волокна from_a.. кабеля A с волокнами from_b.. кабеля B. Всё или ничего."""
    if count < 1:
        raise EditError("Количество волокон — от 1")
    fibers_a = _fibers(db, cable_a_id, from_a, count)
    fibers_b = _fibers(db, cable_b_id, from_b, count)
    try:
        with db.begin_nested():
            for fa, fb in zip(fibers_a, fibers_b, strict=True):
                add_splice(db, clo, fa.id, fb.id, end_a=end_a, end_b=end_b, note=note, user_id=user_id)
    except EditError as exc:
        raise EditError(f"Ничего не сварено: {exc}") from None
    return count


def _fibers(db: Session, cable_id: int, start: int, count: int) -> list[Fiber]:
    cable = db.get(Cable, cable_id)
    if cable is None:
        raise EditError("Кабель не найден")
    fibers = db.scalars(sa.select(Fiber).where(Fiber.cable_id == cable_id, Fiber.number >= start,
                                               Fiber.number < start + count).order_by(Fiber.number)).all()
    if start < 1 or len(fibers) < count:
        raise EditError(f"В кабеле «{cable.name}» нет волокон {start}–{start + count - 1}")
    return list(fibers)


def update_splice(db: Session, sp: Splice, changes: dict, user_id: int | None) -> Splice:
    allowed = {"loss_splicer_db", "loss_otdr_ab_db", "loss_otdr_ba_db", "note"}
    before = {f: getattr(sp, f) for f in allowed}
    for k, v in changes.items():
        if k not in allowed:
            raise EditError(f"Поле «{k}» менять нельзя")
        if k.startswith("loss") and v is not None and not -1 < v < 5:
            raise EditError("Потери — от 0 до 5 дБ (по рефлектометру с одной стороны бывают отрицательными)")
        setattr(sp, k, (v.strip() or None) if isinstance(v, str) else v)
    db.flush()
    audit(db, user_id, "update", "splice", sp.id, before, {f: getattr(sp, f) for f in allowed})
    return sp


def delete_splice(db: Session, sp: Splice, user_id: int | None) -> None:
    audit(db, user_id, "unsplice", "closure", sp.closure_id, {
        "splice_id": sp.id, "a": f"{sp.fiber_a.cable.name} ОВ {sp.fiber_a.number}",
        "b": f"{sp.fiber_b.cable.name} ОВ {sp.fiber_b.number}"}, None)
    db.delete(sp)
    db.flush()


# ------------------------------------------------------------------ разрезание кабеля в муфте


def split_cable(db: Session, cable: Cable, clo: Closure, *, new_name: str | None = None,
                transit: bool = True, user_id: int | None = None) -> Cable:
    """Разрезать кабель в узле муфты: до узла — исходный кабель, после — новый.

    Всё, что было подключено к концу `b` исходного кабеля (сварки в дальних муфтах, порты
    кросса), переходит к новому кабелю; запасы после узла — тоже. При `transit` все волокна
    сразу соединяются транзитными сварками, то есть оптически ничего не меняется.
    """
    node_id = clo.node_id
    steps = db.execute(sa.text("""
        SELECT cr.id, cr.seq, CASE WHEN cr.direction = 1 THEN s.node_b_id ELSE s.node_a_id END
        FROM cable_route cr JOIN route_segment s ON s.id = cr.segment_id
        WHERE cr.cable_id = :c ORDER BY cr.seq"""), {"c": cable.id}).all()
    cut = next((i for i, (_, _, to_node) in enumerate(steps[:-1]) if to_node == node_id), None)
    if cut is None:
        raise EditError(f"Кабель «{cable.name}» не проходит через этот узел транзитом")
    tail = steps[cut + 1:]
    tail_nodes = {to_node for _, _, to_node in tail}

    new = Cable(name=(new_name or "").strip() or f"{cable.name} (от {clo.number})", operator_id=cable.operator_id,
                cable_type_id=cable.cable_type_id, twist_factor=cable.twist_factor, start_node_id=node_id,
                end_node_id=cable.end_node_id, lay_year=cable.lay_year, note=cable.note,
                source_kind=SourceKind.manual)
    new.fibers = make_fibers(cable.cable_type)
    db.add(new)
    db.flush()
    # Маршрут: хвост переносится в новый кабель с нумерацией с 1
    for k, (route_id, _, _) in enumerate(tail, 1):
        db.execute(sa.text("UPDATE cable_route SET cable_id = :n, seq = :s WHERE id = :id"),
                   {"n": new.id, "s": k, "id": route_id})
    # Запасы в узлах после разреза (в самом узле разреза запас остаётся у исходного кабеля)
    db.execute(sa.update(CableReserve).where(CableReserve.cable_id == cable.id, CableReserve.node_id.in_(tail_nodes),
                                             CableReserve.node_id != node_id).values(cable_id=new.id))
    # Подключения конца b: волокно N исходного → волокно N нового
    old_by_no = {f.number: f.id for f in cable.fibers}
    new_by_no = {f.number: f.id for f in new.fibers}
    remap = {old_by_no[n]: new_by_no[n] for n in old_by_no}
    for sp in db.scalars(sa.select(Splice).where(sa.or_(
            sa.and_(Splice.fiber_a_id.in_(remap), Splice.end_a == FiberEnd.b),
            sa.and_(Splice.fiber_b_id.in_(remap), Splice.end_b == FiberEnd.b)))):
        if sp.end_a == FiberEnd.b and sp.fiber_a_id in remap:
            sp.fiber_a_id = remap[sp.fiber_a_id]
        if sp.end_b == FiberEnd.b and sp.fiber_b_id in remap:
            sp.fiber_b_id = remap[sp.fiber_b_id]
    b_ports = sa.select(CrossPort).where(CrossPort.fiber_id.in_(remap), CrossPort.fiber_end == FiberEnd.b)
    for port in db.scalars(b_ports):
        port.fiber_id = remap[port.fiber_id]
    cable.end_node_id = node_id
    _flush(db)
    if transit:
        _flush(db, *(Splice(closure_id=clo.id, fiber_a_id=old_by_no[n], end_a=FiberEnd.b, fiber_b_id=new_by_no[n],
                            end_b=FiberEnd.a, note=TRANSIT_NOTE) for n in sorted(old_by_no)))
    db.expire_all()
    refresh_cable_geometry(db, [cable.id, new.id])
    audit(db, user_id, "split", "cable", cable.id, None,
          {"closure_id": clo.id, "new_cable_id": new.id, "new_name": new.name, "transit": transit})
    return new


# ------------------------------------------------------------------ кроссы

CROSS_FIELDS = ["name", "model", "connector", "operator_id"]


def create_cross(db: Session, *, node_id: int, name: str, port_count: int, model: str | None = None,
                 connector: str | None = None, operator_id: int | None = None,
                 user_id: int | None = None) -> OpticalCross:
    if db.get(NetworkNode, node_id) is None:
        raise EditError("Узел не найден")
    if not name.strip():
        raise EditError("Укажите название кросса")
    if not 1 <= port_count <= 288:
        raise EditError("Портов — от 1 до 288")
    x = OpticalCross(node_id=node_id, name=name.strip(), model=model, port_count=port_count, connector=connector,
                     operator_id=operator_id, source_kind=SourceKind.manual)
    x.ports = [CrossPort(port_no=k) for k in range(1, port_count + 1)]
    db.add(x)
    db.flush()
    audit(db, user_id, "create", "optical_cross", x.id, None,
          {**{f: getattr(x, f) for f in CROSS_FIELDS}, "port_count": port_count})
    return x


def update_cross(db: Session, x: OpticalCross, changes: dict, user_id: int | None) -> OpticalCross:
    before = {**{f: getattr(x, f) for f in CROSS_FIELDS}, "port_count": x.port_count}
    for k, v in changes.items():
        if k == "port_count":
            _resize_ports(db, x, int(v))
        elif k in CROSS_FIELDS:
            setattr(x, k, v.strip() if isinstance(v, str) else v)
        else:
            raise EditError(f"Поле «{k}» менять нельзя")
    db.flush()
    audit(db, user_id, "update", "optical_cross", x.id, before,
          {**{f: getattr(x, f) for f in CROSS_FIELDS}, "port_count": x.port_count})
    return x


def _resize_ports(db: Session, x: OpticalCross, count: int) -> None:
    if not 1 <= count <= 288:
        raise EditError("Портов — от 1 до 288")
    if count < x.port_count:
        busy = [p.port_no for p in x.ports if p.port_no > count and p.fiber_id is not None]
        if busy:
            raise EditError(f"Порты {', '.join(map(str, busy[:10]))} заняты — сначала отключите их")
        for p in [p for p in x.ports if p.port_no > count]:
            x.ports.remove(p)
    else:
        x.ports.extend(CrossPort(port_no=k) for k in range(x.port_count + 1, count + 1))
    x.port_count = count


def delete_cross(db: Session, x: OpticalCross, user_id: int | None) -> int:
    used = sum(1 for p in x.ports if p.fiber_id is not None)
    audit(db, user_id, "delete", "optical_cross", x.id, {"name": x.name, "node_id": x.node_id, "connected": used})
    db.delete(x)
    db.flush()
    return used


def connect_ports(db: Session, x: OpticalCross, *, cable_id: int, fiber_from: int, port_from: int, count: int,
                  end: str | None = None, user_id: int | None = None) -> int:
    """Завести волокна кабеля на порты кросса подряд (волокно fiber_from → порт port_from, …)."""
    fibers = _fibers(db, cable_id, fiber_from, count)
    ports = {p.port_no: p for p in x.ports}
    if port_from < 1 or port_from + count - 1 > x.port_count:
        raise EditError(f"У кросса порты 1–{x.port_count}")
    try:
        with db.begin_nested():
            for k, f in enumerate(fibers):
                port = ports[port_from + k]
                if port.fiber_id is not None:
                    raise EditError(f"Порт {port.port_no} уже занят")
                port.fiber_id, port.fiber_end = f.id, _end_at(db, f, x.node_id, end)
                db.flush()
    except EditError as exc:
        raise EditError(f"Ничего не подключено: {exc}") from None
    except sa.exc.DBAPIError as exc:
        raise EditError(f"Ничего не подключено: {_db_error(exc)}") from None
    audit(db, user_id, "connect", "optical_cross", x.id, None,
          {"cable": fibers[0].cable.name, "fibers": f"{fiber_from}–{fiber_from + count - 1}",
           "ports": f"{port_from}–{port_from + count - 1}"})
    return count


def disconnect_ports(db: Session, x: OpticalCross, port_from: int, count: int, user_id: int | None) -> int:
    n = 0
    for p in x.ports:
        if port_from <= p.port_no < port_from + count and p.fiber_id is not None:
            p.fiber_id, p.fiber_end = None, None
            n += 1
    db.flush()
    audit(db, user_id, "disconnect", "optical_cross", x.id, None, {"ports": f"{port_from}–{port_from + count - 1}",
                                                                   "disconnected": n})
    return n


# ------------------------------------------------------------------ лист рассветки


def _hex_map(db: Session) -> dict[str, str]:
    return dict(db.execute(sa.text("SELECT DISTINCT ON (color_name) color_name, hex FROM color_scheme_entry")).all())


def closure_sheet(db: Session, clo: Closure) -> dict:
    """Всё для редактора рассветки: кабели с волокнами и их состоянием, сварки, замечания."""
    hexes = _hex_map(db)
    ends = cable_ends_at(db, clo.node_id)
    fiber_ids = [f.id for ce in ends for f in ce.cable.fibers]
    # Кто занимает каждый конец волокна в этом узле: сварка (в этой или другой муфте узла) или порт кросса
    used: dict[tuple[int, str], dict] = {}
    for sp in db.scalars(sa.select(Splice).where(sa.or_(Splice.fiber_a_id.in_(fiber_ids),
                                                        Splice.fiber_b_id.in_(fiber_ids)))):
        for mine, my_end, other in ((sp.fiber_a, sp.end_a, sp.fiber_b), (sp.fiber_b, sp.end_b, sp.fiber_a)):
            used[(mine.id, my_end.value)] = {
                "state": "splice" if sp.closure_id == clo.id else "elsewhere", "splice_id": sp.id,
                "partner": {"cable_id": other.cable_id, "cable": other.cable.name, "fiber": other.number},
                "closure_id": sp.closure_id}
    for port in db.scalars(sa.select(CrossPort).where(CrossPort.fiber_id.in_(fiber_ids))):
        used[(port.fiber_id, port.fiber_end.value)] = {
            "state": "port", "cross_id": port.cross_id, "label": f"{port.cross.name}, порт {port.port_no}"}

    cables = []
    for ce in ends:
        fibers = []
        for f in ce.cable.fibers:
            u = used.get((f.id, ce.end.value), {"state": "free"})
            fibers.append({"id": f.id, "number": f.number, "module_no": f.module_no, "in_module": f.number_in_module,
                           "color": f.color, "hex": hexes.get(f.color or "", "#cccccc"),
                           "module_color": f.module_color, "module_hex": hexes.get(f.module_color or "", "#999999"),
                           **u})
        cables.append({"id": ce.cable.id, "name": ce.cable.name, "end": ce.end.value,
                       "operator": ce.cable.operator.short_name, "mark": ce.cable.cable_type.mark,
                       "fibers": fibers, "free": sum(1 for f in fibers if f["state"] == "free")})

    splices = []
    for sp in sorted(clo.splices, key=lambda s: (s.fiber_a.cable.name, s.fiber_a.number)):
        splices.append({"id": sp.id, "note": sp.note, "loss_db": sp.loss_splicer_db,
                        "loss_otdr_ab_db": sp.loss_otdr_ab_db, "loss_otdr_ba_db": sp.loss_otdr_ba_db,
                        "a": _fiber_ref(sp.fiber_a, sp.end_a, hexes), "b": _fiber_ref(sp.fiber_b, sp.end_b, hexes)})

    transit = transit_cables(db, clo.node_id)
    issues = []
    for c in transit:
        issues.append({"level": "info", "cable_id": c.id,
                       "message": f"Кабель «{c.name}» проходит через узел без разреза. Чтобы сварить его волокна, "
                                  "разрежьте его в этой муфте."})
    for s in splices:
        if s["loss_db"] is not None and s["loss_db"] > LOSS_WARN_DB:
            issues.append({"level": "warning", "splice_id": s["id"],
                           "message": f"Сварка {s['a']['cable']} ОВ {s['a']['fiber']} ↔ {s['b']['cable']} ОВ "
                                      f"{s['b']['fiber']}: потери {s['loss_db']:.2f} дБ — выше {LOSS_WARN_DB} дБ"})
    for c in cables:
        if c["free"] and c["free"] < len(c["fibers"]):
            issues.append({"level": "info", "cable_id": c["id"],
                           "message": f"«{c['name']}»: не подключено волокон — {c['free']} из {len(c['fibers'])}"})
    node = clo.node
    return {
        "closure": {"id": clo.id, "number": clo.number, "model": clo.model, "operator_id": clo.operator_id,
                    "note": clo.note, "node": {"id": node.id, "label": node.code or node.name or f"#{node.id}"}},
        "cables": cables,
        "transit": [{"id": c.id, "name": c.name, "mark": c.cable_type.mark} for c in transit],
        "splices": splices,
        "issues": issues,
    }


def _fiber_ref(f: Fiber, end: FiberEnd, hexes: dict) -> dict:
    return {"fiber_id": f.id, "cable_id": f.cable_id, "cable": f.cable.name, "fiber": f.number, "end": end.value,
            "module_no": f.module_no, "color": f.color, "hex": hexes.get(f.color or "", "#cccccc"),
            "module_color": f.module_color, "module_hex": hexes.get(f.module_color or "", "#999999")}


def cross_sheet(db: Session, x: OpticalCross) -> dict:
    hexes = _hex_map(db)
    ends = cable_ends_at(db, x.node_id)
    ports = []
    for p in x.ports:
        f = p.fiber
        ports.append({"port_no": p.port_no, "note": p.note,
                      "fiber": None if f is None else {
                          "fiber_id": f.id, "cable_id": f.cable_id, "cable": f.cable.name, "fiber": f.number,
                          "end": p.fiber_end.value, "color": f.color, "hex": hexes.get(f.color or "", "#cccccc")}})
    return {"cross": {"id": x.id, "name": x.name, "model": x.model, "connector": x.connector,
                      "port_count": x.port_count, "operator_id": x.operator_id, "node_id": x.node_id},
            "ports": ports,
            "cables": [{"id": ce.cable.id, "name": ce.cable.name, "end": ce.end.value,
                        "fibers": ce.cable.cable_type.fiber_count} for ce in ends]}


def cable_route_ok(db: Session, cable_id: int) -> bool:
    """Маршрут непрерывен: конец каждого шага совпадает с началом следующего (для проверок в тестах)."""
    rows = db.execute(sa.text("""
        SELECT CASE WHEN cr.direction = 1 THEN s.node_a_id ELSE s.node_b_id END,
               CASE WHEN cr.direction = 1 THEN s.node_b_id ELSE s.node_a_id END
        FROM cable_route cr JOIN route_segment s ON s.id = cr.segment_id WHERE cr.cable_id = :c ORDER BY cr.seq"""),
        {"c": cable_id}).all()
    c = db.get(Cable, cable_id)
    return bool(rows) and rows[0][0] == c.start_node_id and rows[-1][1] == c.end_node_id and all(
        rows[i][1] == rows[i + 1][0] for i in range(len(rows) - 1))

