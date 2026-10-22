"""Эксперимент на сгенерированной сети: насколько точно система находит повреждение.

Генератор хранит истинные длины участков и запасов и истинную укрутку кабелей (sim_truth),
а в рабочие поля пишет их с ошибкой, соответствующей источнику. Это позволяет честно
проверить локализацию:

1. выбрать тракт «порт кросса узла → порт кросса здания» и случайную точку на нём;
2. по истинным значениям вычислить, что показал бы рефлектометр (+ шум прибора);
3. локализовать по рабочим (неточным) данным, как это сделала бы система в жизни;
4. сравнить: ошибка в метрах по трассе и попала ли истинная точка в интервал.

Доля попаданий в интервал — проверка честности интервала: при 2σ ожидается ≈ 95 %.
"""
import random
import statistics
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import CrossPort, OpticalCross, SimTruth, SourceKind
from app.services import localization, tracing


@dataclass
class Trial:
    port_id: int
    true_pos: float        # истинное положение в «рабочих» метрах тракта (для сравнения с интервалом)
    pred_pos: float
    half_width: float
    error_m: float         # |прогноз − истина| по трассе, м
    inside: bool


def _truth(db: Session) -> dict:
    return {(t.entity_type, t.entity_id, t.attribute): t.value for t in db.scalars(sa.select(SimTruth))}


def measurement_ports(db: Session) -> list[int]:
    """Порты кроссов узлов связи с подключённым волокном, путь от которых кончается портом."""
    ports = db.scalars(sa.select(CrossPort.id).join(OpticalCross).where(
        CrossPort.fiber_id.is_not(None), OpticalCross.name == "ODF-1", OpticalCross.port_count > 8)
        .order_by(CrossPort.id)).all()
    return list(ports)


def has_truth(db: Session) -> bool:
    return db.scalar(sa.select(sa.func.count(SimTruth.id))) > 0


def _traces(db: Session) -> dict[int, tracing.Trace]:
    traces = {}
    for pid in measurement_ports(db):
        fiber, end, port = tracing.start_of_port(db, pid)
        tr = tracing.trace(db, fiber.id, end, start_port=port)
        if tr.end_state == "port" and tr.elements:
            traces[pid] = tr
    if not traces:
        raise RuntimeError("Нет трактов «порт → порт» для эксперимента")
    return traces


def run(db: Session, n: int = 200, seed: int = 1, otdr_noise: bool = True) -> dict:
    rnd = random.Random(seed)
    truth = _truth(db)
    if not truth:
        raise RuntimeError("Нет истинных значений: эксперимент возможен только на сгенерированной сети")
    traces = _traces(db)
    trials = []
    pids = sorted(traces)
    for _ in range(n):
        tr = traces[rnd.choice(pids)]
        trials.append(_trial(tr, truth, rnd, otdr_noise))
    errors = [t.error_m for t in trials]
    widths = [t.half_width for t in trials]
    return {
        "trials": n, "paths": len(traces),
        "error_mean_m": round(statistics.mean(errors), 1),
        "error_median_m": round(statistics.median(errors), 1),
        "error_p90_m": round(sorted(errors)[int(0.9 * (len(errors) - 1))], 1),
        "error_max_m": round(max(errors), 1),
        "half_width_mean_m": round(statistics.mean(widths), 1),
        "coverage_pct": round(100 * sum(t.inside for t in trials) / n, 1),
    }


def _fault(tr: tracing.Trace, truth: dict, rnd: random.Random, otdr_noise: bool):
    """Случайное повреждение на тракте: (элемент, доля, показание рефлектометра, истинные длины)."""
    # Истинные длины и укрутка для каждого элемента тракта
    true_len, true_tw = [], []
    for e in tr.elements:
        key = (("route_segment", e.segment_id, "length_m") if e.kind == "segment"
               else ("cable_reserve", e.reserve_id, "length_m"))
        true_len.append(truth.get(key, e.length_m))
        true_tw.append(truth.get(("cable", e.cable_id, "twist_factor"), e.twist))
    # Случайная точка внутри случайного участка (вероятность пропорциональна длине)
    seg_idx = [i for i, e in enumerate(tr.elements) if e.kind == "segment"]
    weights = [true_len[i] for i in seg_idx]
    i = rnd.choices(seg_idx, weights)[0]
    frac = rnd.random()
    true_opt = sum(true_len[j] * (1 + true_tw[j]) for j in range(i)) + frac * true_len[i] * (1 + true_tw[i])
    reading = true_opt
    if otdr_noise:
        reading += rnd.gauss(0, (localization.OTDR_ERR_BASE_M + localization.OTDR_ERR_REL * true_opt) / 2)
    return i, frac, max(reading, 0.1), true_len


def _trial(tr: tracing.Trace, truth: dict, rnd: random.Random, otdr_noise: bool) -> Trial:
    i, frac, reading, true_len = _fault(tr, truth, rnd, otdr_noise)
    res = localization.localize(tr, reading)
    e = tr.elements[i]
    true_pos = e.start_phys + frac * e.length_m   # та же доля участка — в рабочих метрах
    pred, h = res["position_m"], res["half_width_m"]
    # Ошибку меряем по «настоящей» трассе: переводим обе позиции в истинные метры
    err = abs(_to_true(tr, true_len, pred) - _to_true(tr, true_len, true_pos))
    return Trial(0, true_pos, pred, h, err, pred - h <= true_pos <= pred + h)


def _to_true(tr: tracing.Trace, true_len: list[float], pos: float) -> float:
    acc_true = 0.0
    for e, tl in zip(tr.elements, true_len, strict=True):
        if pos <= e.start_phys + e.length_m:
            f = (pos - e.start_phys) / e.length_m if e.length_m else 0.0
            return acc_true + f * tl
        acc_true += tl
    return acc_true


def run_experiment(db: Session, n: int = 200, seed: int = 1) -> dict:
    return run(db, n=n, seed=seed)


def simulate_incidents(db: Session, n: int = 30, seed: int = 11, user_id: int | None = None) -> int:
    """Заполнить журнал устранёнными повреждениями на сгенерированной сети (для демонстрации и калибровки).

    Для каждого: случайное место, показание прибора по истинным длинам, прогноз по рабочим
    данным (как в жизни), затем «бригада нашла» — фактическое место ставится в истинную точку.
    """
    from app.services import incidents  # здесь, чтобы не было циклического импорта

    rnd = random.Random(seed)
    truth = _truth(db)
    if not truth:
        raise RuntimeError("Моделирование повреждений возможно только на сгенерированной сети")
    traces = _traces(db)
    pids = sorted(traces)
    now = datetime.now(UTC)
    for k in range(n):
        pid = rnd.choice(pids)
        tr = traces[pid]
        i, frac, reading, _ = _fault(tr, truth, rnd, True)
        res = localization.localize(tr, reading)
        detected = now - timedelta(days=rnd.uniform(1, 180), hours=rnd.uniform(0, 23))
        inc = localization.save_incident(db, res, title=f"Тестовое повреждение {k + 1}", fiber_id=tr.legs[0].fiber_id,
                                         port_id=pid, distance_m=round(reading, 1), ior=None, user_id=user_id,
                                         source_kind=SourceKind.generator, detected_at=detected)
        lon, lat = localization.point_at(tr.elements[i].coords, frac)
        incidents.set_actual(db, inc, lon, lat, restored_at=detected + timedelta(hours=rnd.uniform(2, 9)),
                             user_id=user_id)
    return n


def sigma_check(result: dict) -> str:
    """Короткий вывод для отчёта."""
    cov = result["coverage_pct"]
    verdict = "интервал честный" if 90 <= cov <= 99.5 else ("интервал занижен" if cov < 90 else "интервал завышен")
    return f"Попадание в интервал {cov} % при ожидаемых ≈95 % — {verdict}"

