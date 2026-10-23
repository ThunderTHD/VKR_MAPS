"""Экспериментальная оценка системы для ВКР (этап 12).

Все опыты — на сгенерированных сетях: генератор знает истинные длины участков и запасов и
истинную укрутку кабелей (sim_truth), а в рабочие поля пишет их с ошибкой, свойственной
источнику длины. Поэтому каждую оценку системы можно сравнить с правдой.

Серии опытов:
  E1. Точность локализации повреждения: система (до и после калибровки) против двух
      «ручных» способов, которыми пользуются без системы: отмерить показание рефлектометра
      по линии на карте и отмерить его по длинам из документации (без укрутки и запасов).
      Повреждения одни и те же для всех способов (парное сравнение).
  E2. Влияние качества исходных данных: доля участков с измеренной длиной — от «только
      карта» до «все длины из ИД».
  E3. Кривая обучения калибровки: ошибка после калибровки в зависимости от числа
      устранённых повреждений с отмеченным фактическим местом.
  E4. Поиск места обрыва по аварии узлов доступа (корреляция): доля случаев, когда настоящий
      участок оказался в лучшем отрезке, длина отрезка; с неполными данными и ложными
      срабатываниями мониторинга.
  E5. Быстродействие на сетях разного размера.

Каждая серия считается на нескольких сетях (разные seed), повреждения — случайные с
фиксированным seed: результат воспроизводим. Ошибка измеряется по трассе в истинных метрах.
"""
import random
import statistics
import threading
import time
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import AccessNode, NodeStatus
from app.services import calibration, experiment, impact, localization, tracing
from app.services.generator import GeneratorParams, NetworkGenerator, reset_network

Progress = Callable[[str, float], None]

METHODS = {
    "map": "По линии на карте (вручную)",
    "docs": "По длинам из документации (вручную)",
    "system": "Система, без калибровки",
    "calibrated": "Система после калибровки",
}
SCENARIOS = {
    "map_only": ("Только карта (длин нет)", {"geometry": 1.0}),
    "current": ("Как в жизни: 70 % длин известно", None),
    "half": ("Половина длин измерена", {"reflectometer": 0.25, "as_built": 0.25, "geometry": 0.5}),
    "as_built": ("Все длины из ИД", {"as_built": 1.0}),
}
SUITES = {
    # seeds — сети; faults — повреждений на сеть; curve — точки кривой обучения
    "quick": {"seeds": [42], "faults": 60, "curve": [0, 5, 15, 40], "cuts": 40, "sizes": [150, 300],
              "nodes": 220, "incidents": 25},
    "full": {"seeds": [42, 7, 2026], "faults": 200, "curve": [0, 3, 5, 10, 20, 40, 80], "cuts": 150,
             "sizes": [150, 300, 600, 1200], "nodes": 300, "incidents": 30},
}


@dataclass
class Fault:
    pid: int            # порт, с которого меряют
    i: int              # индекс элемента тракта с повреждением
    frac: float
    reading: float      # показание рефлектометра (оптические метры)
    true_len: list[float]
    true_pos: float     # истинное положение по трассе, истинные метры


# ------------------------------------------------------------------ общие помощники


def build(db: Session, seed: int, nodes: int, **kw) -> None:
    reset_network(db)
    NetworkGenerator(db, GeneratorParams(nodes=nodes, seed=seed, **kw)).run()
    db.flush()
    db.execute(sa.text("DELETE FROM calibration_run"))     # калибровка — только из этого опыта


def make_faults(db: Session, traces: dict[int, tracing.Trace], n: int, seed: int) -> list[Fault]:
    truth = experiment._truth(db)
    rnd = random.Random(seed)
    pids = sorted(traces)
    out = []
    for _ in range(n):
        pid = rnd.choice(pids)
        i, frac, reading, true_len = experiment._fault(traces[pid], truth, rnd, True)
        out.append(Fault(pid, i, frac, reading, true_len, sum(true_len[:i]) + frac * true_len[i]))
    return out


def _true_at(true_len: list[float], idx: int, frac: float) -> float:
    return sum(true_len[:idx]) + frac * true_len[idx]


def by_system(tr: tracing.Trace, f: Fault) -> tuple[float, float, bool]:
    """(ошибка, полуширина интервала, попала ли истина в интервал)."""
    res = localization.localize(tr, f.reading)
    pred, h = res["position_m"], res["half_width_m"]
    e = tr.elements[f.i]
    true_work = e.start_phys + f.frac * e.length_m
    err = abs(experiment._to_true(tr, f.true_len, pred) - f.true_pos)
    return err, h, pred - h <= true_work <= pred + h


def by_hand(tr: tracing.Trace, f: Fault, geom_len: dict[int, float], use_docs: bool) -> float:
    """Ручной способ: показание прибора отмеряют по трассе как физическую длину — без укрутки и запасов.

    use_docs=False — длины участков по нарисованной линии; True — из документации там, где они есть.
    """
    acc = 0.0
    for idx, e in enumerate(tr.elements):
        if e.kind != "segment":
            continue
        length = (e.length_m if e.raw_geom_m is None else e.raw_geom_m) if use_docs else geom_len[e.segment_id]
        if acc + length >= f.reading:
            frac = (f.reading - acc) / length if length else 0.0
            return abs(_true_at(f.true_len, idx, frac) - f.true_pos)
        acc += length
    return abs(sum(f.true_len) - f.true_pos)


def summary(errors: list[float], widths: list[float] | None = None, inside: list[bool] | None = None) -> dict:
    s = sorted(errors)
    out = {"n": len(s), "mean_m": round(statistics.mean(s), 1), "median_m": round(statistics.median(s), 1),
           "p90_m": round(s[int(0.9 * (len(s) - 1))], 1), "max_m": round(s[-1], 1),
           "within_10m_pct": round(100 * sum(e <= 10 for e in s) / len(s), 1),
           "within_25m_pct": round(100 * sum(e <= 25 for e in s) / len(s), 1)}
    if widths:
        out["half_width_mean_m"] = round(statistics.mean(widths), 1)
    if inside:
        out["coverage_pct"] = round(100 * sum(inside) / len(inside), 1)
    return out


def _geom_lengths(db: Session) -> dict[int, float]:
    return dict(db.execute(sa.text("SELECT id, ST_Length(geom::geography) FROM route_segment")).all())


def _calibrate(db: Session, n_incidents: int, seed: int) -> dict | None:
    """Смоделировать устранённые повреждения, рассчитать и применить калибровку."""
    if n_incidents:
        experiment.simulate_incidents(db, n=n_incidents, seed=seed)
    try:
        cr = calibration.run(db, simulate=False, seed=seed)
    except Exception as exc:  # noqa: BLE001 — мало данных: остаёмся без калибровки
        return {"skipped": str(exc)}
    calibration.apply(db, cr, None)
    p = cr.params
    return {"map_stretch": p["map_stretch"], "observations": p["observations"], "marks": len(cr.proposals)}


# ------------------------------------------------------------------ E1, E2: точность локализации


def compare_methods(db: Session, seed: int, cfg: dict, length_mix: dict | None = None) -> dict:
    """Один опыт: сеть → повреждения → четыре способа на одних и тех же повреждениях."""
    build(db, seed, cfg["nodes"], length_mix=length_mix)
    geom = _geom_lengths(db)
    before = experiment._traces(db)
    faults = make_faults(db, before, cfg["faults"], seed + 1000)
    res: dict[str, dict] = {k: {"err": [], "w": [], "in": []} for k in METHODS}
    for f in faults:
        tr = before[f.pid]
        res["map"]["err"].append(by_hand(tr, f, geom, use_docs=False))
        res["docs"]["err"].append(by_hand(tr, f, geom, use_docs=True))
        e, h, ok = by_system(tr, f)
        res["system"]["err"].append(e)
        res["system"]["w"].append(h)
        res["system"]["in"].append(ok)
    with db.begin_nested() as sp:
        cal = _calibrate(db, cfg["incidents"], seed + 2000)
        after = experiment._traces(db)
        for f in faults:
            e, h, ok = by_system(after[f.pid], f)
            res["calibrated"]["err"].append(e)
            res["calibrated"]["w"].append(h)
            res["calibrated"]["in"].append(ok)
        sp.rollback()
    return {"raw": res, "calibration": cal, "paths": len(before)}


def e1_methods(db: Session, cfg: dict, progress: Progress) -> dict:
    pooled: dict[str, dict] = {k: {"err": [], "w": [], "in": []} for k in METHODS}
    per_seed = []
    for k, seed in enumerate(cfg["seeds"]):
        progress(f"E1: сеть {k + 1} из {len(cfg['seeds'])}", k / len(cfg["seeds"]))
        r = compare_methods(db, seed, cfg)
        per_seed.append({"seed": seed, "paths": r["paths"], "calibration": r["calibration"],
                         **{m: round(statistics.median(r["raw"][m]["err"]), 1) for m in METHODS}})
        for m in METHODS:
            for key in ("err", "w", "in"):
                pooled[m][key] += r["raw"][m][key]
    rows = []
    for m, title in METHODS.items():
        rows.append({"method": m, "title": title, **summary(pooled[m]["err"], pooled[m]["w"] or None,
                                                             pooled[m]["in"] or None)})
    base = next(r for r in rows if r["method"] == "map")["median_m"]
    for r in rows:
        r["vs_map_pct"] = round(100 * (1 - r["median_m"] / base), 1) if base else None
    # Распределение ошибок — для гистограммы
    bins = [0, 5, 10, 15, 20, 30, 50, 75, 100, 150, 1e9]
    hist = {m: [sum(bins[j] <= e < bins[j + 1] for e in pooled[m]["err"]) for j in range(len(bins) - 1)]
            for m in METHODS}
    return {"title": "E1. Точность локализации: система против ручных способов", "rows": rows,
            "per_seed": per_seed, "hist": {"bins": [f"{bins[j]:g}–{bins[j + 1]:g}" if bins[j + 1] < 1e8
                                                    else f"≥{bins[j]:g}" for j in range(len(bins) - 1)],
                                           "counts": hist}}


def e2_data_quality(db: Session, cfg: dict, progress: Progress) -> dict:
    rows = []
    for k, (key, (title, mix)) in enumerate(SCENARIOS.items()):
        progress(f"E2: {title}", k / len(SCENARIOS))
        errs: dict[str, list[float]] = {m: [] for m in METHODS}
        widths, cov = [], []
        for seed in cfg["seeds"][:2]:
            r = compare_methods(db, seed, cfg, length_mix=mix)
            for m in METHODS:
                errs[m] += r["raw"][m]["err"]
            widths += r["raw"]["system"]["w"]
            cov += r["raw"]["system"]["in"]
        rows.append({"scenario": key, "title": title,
                     **{f"{m}_median_m": round(statistics.median(errs[m]), 1) for m in METHODS},
                     **{f"{m}_p90_m": summary(errs[m])["p90_m"] for m in METHODS},
                     "half_width_mean_m": round(statistics.mean(widths), 1),
                     "coverage_pct": round(100 * sum(cov) / len(cov), 1)})
    return {"title": "E2. Влияние качества исходных данных (медиана ошибки, м)", "rows": rows}


# ------------------------------------------------------------------ E3: кривая обучения калибровки


def e3_learning_curve(db: Session, cfg: dict, progress: Progress) -> dict:
    points: dict[int, list[float]] = {n: [] for n in cfg["curve"]}
    widths: dict[int, list[float]] = {n: [] for n in cfg["curve"]}
    cov: dict[int, list[bool]] = {n: [] for n in cfg["curve"]}
    for k, seed in enumerate(cfg["seeds"]):
        # Худший случай данных: длины только по карте, измерений из ИД нет — калибровка учится
        # только на устранённых повреждениях (так начинается учёт в любой новой системе)
        build(db, seed, cfg["nodes"], measured_share=0.0, length_mix={"geometry": 1.0})
        before = experiment._traces(db)
        faults = make_faults(db, before, cfg["faults"], seed + 3000)
        for j, n in enumerate(cfg["curve"]):
            progress(f"E3: сеть {k + 1}, повреждений {n}", (k + j / len(cfg["curve"])) / len(cfg["seeds"]))
            with db.begin_nested() as sp:
                if n:
                    _calibrate(db, n, seed + 4000)
                traces = experiment._traces(db)
                for f in faults:
                    e, h, ok = by_system(traces[f.pid], f)
                    points[n].append(e)
                    widths[n].append(h)
                    cov[n].append(ok)
                sp.rollback()
    rows = [{"incidents": n, **summary(points[n], widths[n], cov[n])} for n in cfg["curve"]]
    return {"title": "E3. Калибровка: ошибка в зависимости от числа устранённых повреждений (длины — только по карте)",
            "rows": rows}


# ------------------------------------------------------------------ E4: корреляция аварий


def _seg_lengths(db: Session) -> dict[int, float]:
    return _geom_lengths(db)


def e4_correlation(db: Session, cfg: dict, progress: Progress) -> dict:
    variants = {
        "clean": "Полные данные, мониторинг без ошибок",
        "noise": "Ложное срабатывание: ещё один узел «упал» не из-за обрыва",
        "missing": "У 20 % узлов тракт в системе не заведён",
    }
    stats = {v: {"hit": 0, "hit3": 0, "n": 0, "len": [], "width": [], "conf": Counter()} for v in variants}
    for k, seed in enumerate(cfg["seeds"]):
        progress(f"E4: сеть {k + 1}", k / len(cfg["seeds"]))
        build(db, seed, cfg["nodes"])
        seglen = _seg_lengths(db)
        rnd = random.Random(seed + 5000)
        full = impact.service_paths(db, refresh=True)     # настоящая физика сети: кто через что идёт
        cands = sorted({s for p in full.values() for s in p.segments})
        all_ids = set(db.scalars(sa.select(AccessNode.id)))
        for variant in variants:
            with db.begin_nested() as sp:
                if variant == "missing":
                    ids = sorted(full)
                    drop = rnd.sample(ids, k=max(1, len(ids) // 5))
                    db.execute(sa.update(AccessNode).where(AccessNode.id.in_(drop)).values(cross_port_id=None))
                    db.flush()
                impact.service_paths(db, refresh=True)    # то, что знает система
                for _ in range(max(1, cfg["cuts"] // len(cfg["seeds"]))):
                    seg = rnd.choice(cands)
                    down = {p.access_node_id for p in full.values() if seg in p.segments}
                    if variant == "noise":
                        others = sorted(all_ids - down)
                        if others:
                            down.add(rnd.choice(others))
                    db.execute(sa.update(AccessNode).values(status=NodeStatus.up))
                    res = impact.correlate(db, down)
                    st = stats[variant]
                    st["n"] += 1
                    if res["candidates"]:
                        best = res["candidates"][0]
                        st["hit"] += seg in best["segment_ids"]
                        st["hit3"] += any(seg in c["segment_ids"] for c in res["candidates"][:3])
                        st["len"].append(sum(seglen.get(s, 0) for s in best["segment_ids"]))
                        st["width"].append(len(best["segment_ids"]))
                        st["conf"][best["confidence"]] += 1
                sp.rollback()
            impact._cache.clear()
    rows = []
    for v, title in variants.items():
        st = stats[v]
        rows.append({"variant": v, "title": title, "cuts": st["n"],
                     "hit_pct": round(100 * st["hit"] / st["n"], 1) if st["n"] else None,
                     "hit3_pct": round(100 * st["hit3"] / st["n"], 1) if st["n"] else None,
                     "group_len_median_m": round(statistics.median(st["len"]), 0) if st["len"] else None,
                     "group_segments_median": statistics.median(st["width"]) if st["width"] else None,
                     "confidence": dict(st["conf"])})
    return {"title": "E4. Поиск места обрыва по аварии узлов доступа", "rows": rows}


# ------------------------------------------------------------------ E5: быстродействие


def _timed(fn, repeat: int = 1) -> float:
    t = time.perf_counter()
    for _ in range(repeat):
        fn()
    return (time.perf_counter() - t) / repeat * 1000


def _measure_size(db: Session, size: int) -> dict:
    t0 = time.perf_counter()
    build(db, 42, size, buildings_per_operator=max(4, size // 30))
    gen_s = time.perf_counter() - t0
    counts = dict(db.execute(sa.text("""
        SELECT 'nodes', count(*) FROM network_node UNION ALL SELECT 'segments', count(*) FROM route_segment
        UNION ALL SELECT 'cables', count(*) FROM cable UNION ALL SELECT 'access_nodes', count(*) FROM access_node
    """)).all())
    traces = experiment._traces(db)
    pid = sorted(traces)[len(traces) // 2]
    fiber, end, port = tracing.start_of_port(db, pid)
    tr = traces[pid]
    t_trace = _timed(lambda: tracing.trace(db, fiber.id, end, start_port=port), 5)
    t_loc = _timed(lambda: localization.localize(tr, tr.total_opt / 2), 20)
    impact._cache.clear()
    t_paths = _timed(lambda: impact.service_paths(db, refresh=True))
    t_cached = _timed(lambda: impact.service_paths(db), 5)
    seg = next(iter(next(iter(impact.service_paths(db).values())).segments))
    t_impact = _timed(lambda: impact.impact(db, "segment", seg), 5)
    down = {p.access_node_id for p in impact.service_paths(db).values() if seg in p.segments}
    t_corr = _timed(lambda: impact.correlate(db, down), 5)
    return {"size": size, **counts, "paths": len(traces), "generate_s": round(gen_s, 1),
            "trace_ms": round(t_trace, 1), "localize_ms": round(t_loc, 2),
            "paths_all_ms": round(t_paths), "paths_cached_ms": round(t_cached, 1),
            "impact_ms": round(t_impact, 1), "correlate_ms": round(t_corr, 1)}


def e5_performance(db: Session, cfg: dict, progress: Progress) -> dict:
    rows = []
    for k, size in enumerate(cfg["sizes"]):
        progress(f"E5: сеть ≈{size} точек", k / len(cfg["sizes"]))
        rows.append(_measure_size(db, size))
    return {"title": "E5. Быстродействие", "rows": rows}


# ------------------------------------------------------------------ весь набор


EXPERIMENTS = {"e1": e1_methods, "e2": e2_data_quality, "e3": e3_learning_curve, "e4": e4_correlation,
               "e5": e5_performance}


def run_suite(db: Session, suite: str = "quick", only: list[str] | None = None,
              progress: Progress | None = None, cfg: dict | None = None) -> dict:
    cfg = cfg or SUITES[suite]
    progress = progress or (lambda *_: None)
    out = {"suite": suite, "config": cfg, "results": {}, "timing_s": {}}
    names = only or list(EXPERIMENTS)
    for k, name in enumerate(names):
        t = time.perf_counter()

        def sub(stage: str, frac: float, k=k) -> None:
            progress(stage, (k + min(max(frac, 0.0), 1.0)) / len(names))
        out["results"][name] = EXPERIMENTS[name](db, cfg, sub)
        out["timing_s"][name] = round(time.perf_counter() - t, 1)
    progress("готово", 1.0)
    return out


def _fmt(v) -> str:
    if v is None:
        return "—"
    if isinstance(v, float):
        return f"{v:g}".replace(".", ",")
    return str(v)


def to_markdown(run: dict) -> str:
    """Отчёт для текста ВКР: таблицы всех серий."""
    r = run["results"]
    lines = [f"# Результаты эксперимента (набор «{run['suite']}»)", ""]
    cfg = run["config"]
    lines += [f"Сети: seed {', '.join(map(str, cfg['seeds']))}, ≈{cfg['nodes']} точек; повреждений на сеть: "
              f"{cfg['faults']}; устранённых повреждений для калибровки: {cfg['incidents']}.", ""]
    if "e1" in r:
        lines += [f"## {r['e1']['title']}", "",
                  "| Способ | Медиана, м | Среднее, м | 90 %, м | Макс., м | ≤10 м | ≤25 м | Интервал ±, м | "
                  "Попадание в интервал | Лучше «по карте» |", "|---|---|---|---|---|---|---|---|---|---|"]
        for x in r["e1"]["rows"]:
            lines.append(f"| {x['title']} | {_fmt(x['median_m'])} | {_fmt(x['mean_m'])} | {_fmt(x['p90_m'])} | "
                         f"{_fmt(x['max_m'])} | {_fmt(x['within_10m_pct'])} % | {_fmt(x['within_25m_pct'])} % | "
                         f"{_fmt(x.get('half_width_mean_m'))} | "
                         f"{_fmt(x.get('coverage_pct'))}{' %' if x.get('coverage_pct') is not None else ''} | "
                         f"{_fmt(x['vs_map_pct'])} % |")
        lines.append("")
    if "e2" in r:
        lines += [f"## {r['e2']['title']}", "", "| Данные | По карте | По документации | Система | После калибровки | "
                  "Интервал ±, м | Попадание |", "|---|---|---|---|---|---|---|"]
        for x in r["e2"]["rows"]:
            lines.append(f"| {x['title']} | {_fmt(x['map_median_m'])} | {_fmt(x['docs_median_m'])} | "
                         f"{_fmt(x['system_median_m'])} | {_fmt(x['calibrated_median_m'])} | "
                         f"{_fmt(x['half_width_mean_m'])} | {_fmt(x['coverage_pct'])} % |")
        lines.append("")
    if "e3" in r:
        lines += [f"## {r['e3']['title']}", "", "| Устранённых повреждений | Медиана, м | 90 %, м | Интервал ±, м | "
                  "Попадание |", "|---|---|---|---|---|"]
        for x in r["e3"]["rows"]:
            lines.append(f"| {x['incidents']} | {_fmt(x['median_m'])} | {_fmt(x['p90_m'])} | "
                         f"{_fmt(x['half_width_mean_m'])} | {_fmt(x['coverage_pct'])} % |")
        lines.append("")
    if "e4" in r:
        lines += [f"## {r['e4']['title']}", "", "| Условия | Обрывов | Участок в лучшем отрезке | В трёх лучших | "
                  "Длина отрезка (медиана), м | Участков в отрезке |", "|---|---|---|---|---|---|"]
        for x in r["e4"]["rows"]:
            lines.append(f"| {x['title']} | {x['cuts']} | {_fmt(x['hit_pct'])} % | {_fmt(x['hit3_pct'])} % | "
                         f"{_fmt(x['group_len_median_m'])} | {_fmt(x['group_segments_median'])} |")
        lines.append("")
    if "e5" in r:
        lines += [f"## {r['e5']['title']}", "", "| Точек | Участков | Кабелей | Узлов доступа | Генерация, с | "
                  "Трассировка, мс | Локализация, мс | Пути всех узлов, мс | Из кеша, мс | Что отключится, мс | "
                  "Корреляция, мс |", "|---|---|---|---|---|---|---|---|---|---|---|"]
        for x in r["e5"]["rows"]:
            lines.append(f"| {x['nodes']} | {x['segments']} | {x['cables']} | {x['access_nodes']} | "
                         f"{_fmt(x['generate_s'])} | {_fmt(x['trace_ms'])} | {_fmt(x['localize_ms'])} | "
                         f"{_fmt(x['paths_all_ms'])} | {_fmt(x['paths_cached_ms'])} | {_fmt(x['impact_ms'])} | "
                         f"{_fmt(x['correlate_ms'])} |")
        lines.append("")
    lines.append("Время расчёта серий, с: " + ", ".join(f"{k.upper()} — {_fmt(v)}" for k, v in run["timing_s"].items()))
    return "\n".join(lines) + "\n"


# ------------------------------------------------------------------ отдельная база для эксперимента


def experiment_db_url(base_url: str) -> str:
    """Эксперимент генерирует и стирает сети — в отдельной базе «<имя>_experiment», рабочие данные не трогает."""
    url = sa.engine.make_url(base_url)
    return url.set(database=f"{url.database}_experiment").render_as_string(hide_password=False)


def ensure_db(url: str) -> None:
    """Создать базу эксперимента (если её нет) и применить миграции."""
    from alembic import command
    from alembic.config import Config

    backend = Path(__file__).resolve().parents[2]
    target = sa.engine.make_url(url)
    admin = sa.create_engine(target.set(database="postgres"), isolation_level="AUTOCOMMIT")
    with admin.connect() as c:
        if not c.execute(sa.text("SELECT 1 FROM pg_database WHERE datname = :n"), {"n": target.database}).scalar():
            c.execute(sa.text(f'CREATE DATABASE "{target.database}"'))
    admin.dispose()
    cfg = Config(str(backend / "alembic.ini"))
    cfg.set_main_option("script_location", str(backend / "alembic"))
    cfg.attributes["database_url"] = url
    command.upgrade(cfg, "head")


def run_isolated(base_url: str, suite: str = "quick", only: list[str] | None = None,
                 progress: Progress | None = None, cfg: dict | None = None) -> dict:
    from sqlalchemy.orm import sessionmaker
    url = experiment_db_url(base_url)
    ensure_db(url)
    engine = sa.create_engine(url)
    try:
        with sessionmaker(bind=engine)() as db:
            res = run_suite(db, suite, only, progress, cfg)
            db.rollback()
        return res
    finally:
        engine.dispose()
        impact._cache.clear()


# ------------------------------------------------------------------ прогоны в фоне (страница «Эксперимент»)

STALE_HOURS = 6


def start(session_factory, suite: str, only: list[str] | None, user_id: int | None) -> int:
    """Создать запись прогона и запустить расчёт в отдельном потоке. Одновременно — только один прогон."""
    from datetime import UTC, datetime, timedelta

    from app.core.config import get_settings
    from app.models import ExperimentRun
    from app.services.editing import EditError

    if suite not in SUITES:
        raise EditError(f"Набор опытов: {', '.join(SUITES)}")
    if only and set(only) - set(EXPERIMENTS):
        raise EditError(f"Серии: {', '.join(EXPERIMENTS)}")
    with session_factory() as db:
        db.execute(sa.update(ExperimentRun).where(
            ExperimentRun.status == "running",
            ExperimentRun.created_at < datetime.now(UTC) - timedelta(hours=STALE_HOURS))
            .values(status="failed", error="Прогон прервался (приложение перезапускалось?)"))
        if db.scalar(sa.select(sa.func.count(ExperimentRun.id)).where(ExperimentRun.status == "running")):
            db.commit()
            raise EditError("Эксперимент уже идёт — дождитесь окончания")
        run = ExperimentRun(suite=suite, status="running", started_by=user_id,
                            progress={"stage": "подготовка базы эксперимента", "frac": 0.0, "only": only})
        db.add(run)
        db.commit()
        run_id = run.id
    base_url = get_settings().database_url

    def work():
        with session_factory() as db:
            run = db.get(ExperimentRun, run_id)

            def progress(stage: str, frac: float) -> None:
                run.progress = {"stage": stage, "frac": round(frac, 3), "only": only}
                db.commit()
            try:
                res = run_isolated(base_url, suite, only, progress)
                run.results, run.status = res, "done"
            except Exception as exc:  # noqa: BLE001 — ошибку показываем на странице
                run.status, run.error = "failed", f"{type(exc).__name__}: {exc}"
            run.finished_at = datetime.now(UTC)
            db.commit()

    threading.Thread(target=work, daemon=True, name=f"experiment-{run_id}").start()
    return run_id


def run_summary(run) -> dict:
    return {"id": run.id, "suite": run.suite, "status": run.status, "progress": run.progress,
            "created_at": run.created_at.isoformat() if run.created_at else None,
            "finished_at": run.finished_at.isoformat() if run.finished_at else None, "error": run.error,
            "series": sorted((run.results or {}).get("results", {}).keys())}


def to_csv(run: dict, series: str) -> bytes:
    """Строки серии в CSV для Excel (разделитель «;», запятая в дробях, UTF-8 с BOM)."""
    import csv
    import io
    rows = run["results"][series]["rows"]
    keys = list(dict.fromkeys(k for r in rows for k in r if not isinstance(r[k], dict)))
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    w.writerow(keys)
    for r in rows:
        w.writerow([str(r.get(k, "")).replace(".", ",") if isinstance(r.get(k), float) else r.get(k, "")
                    for k in keys])
    return ("﻿" + buf.getvalue()).encode("utf-8")
