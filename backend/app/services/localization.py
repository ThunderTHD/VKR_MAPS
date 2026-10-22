"""Локализация повреждения по расстоянию с рефлектометра.

Вход: тракт (результат трассировки от порта, к которому подключён рефлектометр) и
расстояние до события, которое показал прибор. Выход: точка на карте, участок трассы,
интервал погрешности и разбор, из чего эта погрешность складывается.

Расчёт:
1. Поправки к показанию прибора: длина измерительного шнура (вычитается) и, если в приборе
   был установлен не тот показатель преломления, пересчёт D = D_прибора · n_прибора / n_волокна.
2. Проход по профилю тракта: каждый элемент (участок, запас) имеет оптическую длину
   L · (1 + k), где k — коэффициент укрутки. Элемент, в котором накопленная оптическая
   длина достигает D, содержит повреждение; остаток переводится обратно в физические метры.
3. Внутри участка точка ставится пропорционально: если по паспорту участок 120 м, а
   на карте нарисован 100 м, то 60 м по паспорту — это середина нарисованной линии.
   Повреждение в запасе — точкой узла, где лежит запас.
4. Погрешность (2σ ≈ 95 %): независимые ошибки длин всех пройденных элементов (по
   источнику длины), ошибка укрутки (одна на кабель — внутри кабеля ошибки коррелированы),
   погрешность прибора. Дисперсии складываются, полуширина интервала h = 2·√Σσ².
5. При измерении с обоих концов оценки объединяются с весами 1/σ²: каждая сторона точнее
   там, где ближе к прибору, поэтому объединённый интервал уже любого из двух.
"""
import math
from dataclasses import dataclass, field
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from app.models import Incident, IncidentStatus, SourceKind
from app.services.editing import EditError, audit
from app.services.tracing import Element, Trace, trace_summary

# Погрешность коэффициента укрутки, если он не уточнён калибровкой (2σ, абсолютная).
# Типичный диапазон укрутки модульных кабелей 0,3–1,2 %; по умолчанию в базе 0,5 %.
TWIST_ERR_2S = 0.004
# Погрешность рефлектометра по расстоянию (2σ): ±(1 м + 5·10⁻⁵ · D) — порядок паспортных
# значений приборов класса MTS/AQ7280 с учётом шага дискретизации.
OTDR_ERR_BASE_M = 1.0
OTDR_ERR_REL = 5e-5
DEFAULT_IOR = 1.4683  # G.652.D на 1310 нм


SOURCE_RU = {"reflectometer": "по рефлектометру", "as_built": "по ИД", "passport": "по паспорту",
             "engineer": "со слов инженера", "geometry": "по карте"}


@dataclass
class Located:
    pos: float                     # физическое расстояние от начала тракта, м
    var: float                     # дисперсия положения, м²
    contrib: dict[str, float] = field(default_factory=dict)
    beyond_m: float = 0.0          # насколько показание больше длины тракта


# ------------------------------------------------------------------ геометрия (локальная метрика)


def _dist(a: list[float], b: list[float]) -> float:
    """Расстояние в метрах для коротких отрезков (равнопромежуточная проекция)."""
    k = math.cos(math.radians((a[1] + b[1]) / 2))
    return math.hypot((b[0] - a[0]) * 111_320 * k, (b[1] - a[1]) * 110_574)


def _cum(coords: list[list[float]]) -> list[float]:
    acc = [0.0]
    for i in range(1, len(coords)):
        acc.append(acc[-1] + _dist(coords[i - 1], coords[i]))
    return acc


def point_at(coords: list[list[float]], f: float) -> list[float]:
    """Точка на линии на доле f её длины."""
    f = min(max(f, 0.0), 1.0)
    acc = _cum(coords)
    target = acc[-1] * f
    for i in range(1, len(coords)):
        if acc[i] >= target or i == len(coords) - 1:
            seg = acc[i] - acc[i - 1]
            t = 0.0 if seg == 0 else (target - acc[i - 1]) / seg
            a, b = coords[i - 1], coords[i]
            return [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
    return coords[-1]


def sub_line(coords: list[list[float]], f1: float, f2: float) -> list[list[float]]:
    acc = _cum(coords)
    total = acc[-1] or 1.0
    out = [point_at(coords, f1)]
    out += [c for c, d in zip(coords, acc, strict=True) if f1 * total < d < f2 * total]
    out.append(point_at(coords, f2))
    return out


# ------------------------------------------------------------------ проход по профилю


def _label(tr: Trace, e: Element) -> str:
    a, b = tr.node_labels.get(e.node_id, "?"), tr.node_labels.get(e.to_node_id, "?")
    if e.kind == "reserve":
        return f"запас «{e.cable}» в {a}"
    return f"участок {a} – {b}"


def _walk(tr: Trace, elements: list[Element], d_opt: float) -> Located:
    """Положение по оптическому расстоянию d_opt, отсчитанному от начала списка элементов."""
    phys = opt = 0.0
    contrib: dict[str, float] = {}
    passed_by_cable: dict[int, tuple[float, float, str]] = {}

    def add(key: str, sigma: float) -> None:
        contrib[key] = contrib.get(key, 0.0) + sigma * sigma

    def passed(e: Element, part: float) -> None:
        add(f"{_label(tr, e)}: длина {SOURCE_RU.get(e.source, e.source)}", part * e.err2_m / 2)
        length, tw, name = passed_by_cable.get(e.cable_id, (0.0, e.twist, e.cable))
        passed_by_cable[e.cable_id] = (length + part * e.length_m, tw, name)

    pos = None
    for e in elements:
        if opt + e.opt_len > d_opt:
            part = (d_opt - opt) / e.opt_len if e.opt_len else 0.0
            passed(e, part)
            pos = phys + part * e.length_m
            break
        passed(e, 1.0)
        phys += e.length_m
        opt += e.opt_len
    beyond = 0.0
    if pos is None:
        pos = phys
        last_tw = elements[-1].twist if elements else 0.0
        beyond = (d_opt - opt) / (1 + last_tw)
    for length, tw, name in passed_by_cable.values():
        add(f"укрутка кабеля «{name}»", length * (TWIST_ERR_2S / 2) / (1 + tw))
    add("погрешность рефлектометра", (OTDR_ERR_BASE_M + OTDR_ERR_REL * d_opt) / 2)
    return Located(pos, sum(contrib.values()), contrib, beyond)


def _reversed(tr: Trace) -> list[Element]:
    return list(reversed(tr.elements))


def phys_to_place(tr: Trace, pos: float) -> dict:
    """Физическое расстояние от начала тракта → точка, элемент и описание места."""
    elements = tr.elements
    if not elements:
        raise EditError("У тракта нет ни одного участка — нечего локализовать")
    pos = min(max(pos, 0.0), tr.total_phys)
    for e in elements:
        end = e.start_phys + e.length_m
        if pos <= end or e is elements[-1]:
            inside = pos - e.start_phys
            if e.kind == "reserve":
                return {"kind": "reserve", "point": tr.node_points.get(e.node_id), "node_id": e.node_id,
                        "cable": e.cable, "segment_id": None,
                        "text": f"в запасе кабеля «{e.cable}» в {tr.node_labels.get(e.node_id)} "
                                f"({e.length_m:.0f} м кольцами)"}
            f = inside / e.length_m if e.length_m else 0.0
            a, b = tr.node_labels.get(e.node_id, "?"), tr.node_labels.get(e.to_node_id, "?")
            return {"kind": "segment", "point": point_at(e.coords, f), "segment_id": e.segment_id,
                    "cable": e.cable, "from_node_id": e.node_id, "to_node_id": e.to_node_id,
                    "from_m": round(inside, 1), "to_m": round(e.length_m - inside, 1),
                    "text": f"участок {a} – {b}: {inside:.0f} м от {a}, {e.length_m - inside:.0f} м до {b} "
                            f"(кабель «{e.cable}»)"}
    raise AssertionError("недостижимо")


def interval_line(tr: Trace, lo: float, hi: float) -> list[list[float]]:
    """Ломаная интервала [lo, hi] по трассе (может проходить через несколько участков)."""
    out: list[list[float]] = []
    for e in tr.elements:
        if e.kind != "segment" or e.length_m == 0:
            continue
        s, t = e.start_phys, e.start_phys + e.length_m
        if t < lo or s > hi:
            continue
        f1, f2 = max(0.0, (lo - s) / e.length_m), min(1.0, (hi - s) / e.length_m)
        out += sub_line(e.coords, f1, f2)
    return out


def wells_near(tr: Trace, lo: float, hi: float, pos: float) -> list[dict]:
    """Точки сети на трассе внутри интервала — где смотреть в первую очередь."""
    seen: dict[int, float] = {}
    for e in tr.elements:
        for node, at in ((e.node_id, e.start_phys), (e.to_node_id, e.start_phys + e.length_m)):
            if node is not None and lo <= at <= hi and node not in seen:
                seen[node] = at
    return sorted(({"node_id": n, "label": tr.node_labels.get(n), "point": tr.node_points.get(n),
                    "offset_m": round(at - pos, 1)} for n, at in seen.items()), key=lambda w: abs(w["offset_m"]))


def correct_distance(distance_m: float, *, launch_m: float = 0.0, ior_set: float | None = None,
                     ior_fiber: float | None = None) -> float:
    d = distance_m - launch_m
    if ior_set and ior_fiber:
        d = d * ior_set / ior_fiber
    if d < 0:
        raise EditError("Расстояние меньше длины измерительного шнура")
    return d


def localize(tr: Trace, distance_m: float, *, launch_m: float = 0.0, ior_set: float | None = None,
             ior_fiber: float | None = None, far_distance_m: float | None = None) -> dict:
    """Место повреждения и интервал погрешности. far_distance_m — показание с другого конца тракта."""
    if not tr.elements:
        raise EditError("У тракта нет ни одного участка — нечего локализовать")
    d = correct_distance(distance_m, launch_m=launch_m, ior_set=ior_set, ior_fiber=ior_fiber)
    warnings: list[str] = []
    near = _walk(tr, tr.elements, d)
    if near.beyond_m > 0.5:
        warnings.append(f"Показание больше длины описанного тракта на {near.beyond_m:.0f} м: повреждение за "
                        f"концом пути ({_end_text(tr)}) или в данных ошибка длин/рассветки")
    est = near
    two_sided = None
    if far_distance_m is not None:
        if tr.end_state != "port":
            raise EditError("Измерение с другого конца возможно, только если тракт заканчивается портом кросса")
        d2 = correct_distance(far_distance_m, launch_m=launch_m, ior_set=ior_set, ior_fiber=ior_fiber)
        far = _walk(tr, _reversed(tr), d2)
        far_pos = tr.total_phys - far.pos
        w1, w2 = 1 / near.var, 1 / far.var
        pos = (near.pos * w1 + far_pos * w2) / (w1 + w2)
        contrib = {k: v * (w1 / (w1 + w2)) ** 2 for k, v in near.contrib.items()}
        for k, v in far.contrib.items():
            contrib[k] = contrib.get(k, 0.0) + v * (w2 / (w1 + w2)) ** 2
        est = Located(pos, 1 / (w1 + w2), contrib)
        gap = abs(near.pos - far_pos)
        two_sided = {"from_start_m": round(near.pos, 1), "from_start_half_width_m": round(2 * math.sqrt(near.var), 1),
                     "from_end_m": round(far_pos, 1), "from_end_half_width_m": round(2 * math.sqrt(far.var), 1),
                     "disagreement_m": round(gap, 1)}
        if gap > 2 * math.sqrt(near.var + far.var):
            warnings.append(f"Оценки с двух концов расходятся на {gap:.0f} м — больше суммарной погрешности. "
                            "Вероятно, ошибка в длинах, запасах или рассветке; проверьте трассу.")
    h = 2 * math.sqrt(est.var)
    lo, hi = max(0.0, est.pos - h), min(tr.total_phys, est.pos + h)
    place = phys_to_place(tr, est.pos)
    total_var = sum(est.contrib.values()) or 1.0
    contributions = sorted(({"source": k, "sigma_m": round(math.sqrt(v), 1), "share_pct": round(100 * v / total_var, 1)}
                            for k, v in est.contrib.items() if v > 0), key=lambda c: -c["share_pct"])
    unknown = sum(1 for e in tr.elements if e.kind == "segment" and e.source == "geometry" and e.start_phys < est.pos)
    if unknown:
        word = _plural(unknown, "участок", "участка", "участков")
        warnings.append(f"До места повреждения {unknown} {word} с длиной только по карте — это основной "
                        "источник ошибки; уточните длины по ИД или рефлектометру.")
    return {
        "distance_corrected_m": round(d, 1),
        "position_m": round(est.pos, 1),
        "half_width_m": round(h, 1),
        "point": place["point"],
        "place": place,
        "interval": {"from_m": round(lo, 1), "to_m": round(hi, 1),
                     "start": phys_to_place(tr, lo)["point"], "end": phys_to_place(tr, hi)["point"],
                     "line": interval_line(tr, lo, hi)},
        "wells": wells_near(tr, lo, hi, est.pos),
        "contributions": contributions[:8],
        "two_sided": two_sided,
        "warnings": warnings,
        "trace": trace_summary(tr),
    }


def _plural(n: int, one: str, few: str, many: str) -> str:
    if n % 10 == 1 and n % 100 != 11:
        return one
    return few if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14 else many


def _end_text(tr: Trace) -> str:
    ev = tr.events[-1] if tr.events else None
    return ev.label if ev else "конец"


# ------------------------------------------------------------------ сохранение повреждения


def save_incident(db: Session, result: dict, *, title: str | None, fiber_id: int, port_id: int | None,
                  distance_m: float, ior: float | None, user_id: int | None) -> Incident:
    place = result["place"]
    x, y = result["point"]
    title = (title or "").strip() or f"Повреждение: {place['text']}"[:200]
    inc = Incident(status=IncidentStatus.localized, title=title,
                   fiber_id=fiber_id, measure_port_id=port_id, otdr_distance_m=distance_m, refractive_index=ior,
                   predicted_geom=f"SRID=4326;POINT({x} {y})", predicted_error_m=result["half_width_m"],
                   predicted_segment_id=place.get("segment_id"), detected_at=datetime.now(UTC),
                   calc_details={k: result[k] for k in ("distance_corrected_m", "position_m", "half_width_m", "place",
                                                        "contributions", "two_sided", "warnings", "wells")},
                   source_kind=SourceKind.manual)
    db.add(inc)
    db.flush()
    audit(db, user_id, "create", "incident", inc.id, None, {"title": inc.title, "otdr_distance_m": distance_m,
                                                            "half_width_m": result["half_width_m"]})
    return inc

