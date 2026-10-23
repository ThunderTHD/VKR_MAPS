"""Калибровка модели длин по фактическим данным.

Что уточняется:
- k_t — коэффициент укрутки для каждой марки кабеля, по которой есть данные. Укрутка —
  свойство конструкции кабеля (шаг скрутки модулей), поэтому у кабелей одной марки она
  близка; оценка по марке опирается на все тракты с этой маркой, а не на один-два кабеля,
  и распространяется на кабели марки, по которым измерений нет;
- s — общая поправка «реальная трасса длиннее нарисованной на карте» для участков, длина
  которых известна только по карте (на практике 3–12 %: провисы, обходы, подъёмы в колодцы);
- τ — разброс этой поправки от участка к участку (из него — новая погрешность длины «по карте»).

Наблюдения:
1. Измерение тракта из ИД: оптическая длина D между двумя портами кроссов.
2. Устранённое повреждение: показание рефлектометра D и фактическое место, то есть
   оптическое расстояние до известной точки трассы.

Модель одного наблюдения (сумма по маркам t кабелей на пути до точки):
    D = Σ_t (1 + k_t) · (K_t + (1 + s) · G_t) + ε,
где K_t — сумма известных длин (участки с фактической длиной, запасы), G_t — сумма длин
«по карте», ε — шум: погрешности известных длин, прибора и разброс поправки карты (τ).

Оценка — метод наименьших квадратов с априорными значениями (регуляризация Тихонова,
то же самое, что оценка максимума апостериорной вероятности при нормальных ошибках):
    min Σ_j (D_j − f_j(θ))² / σ_j² + Σ_p (θ_p − θ⁰_p)² / σ⁰_p².
Априорное значение удерживает параметры, по которым данных мало, у разумных величин;
модель нелинейна (произведение (1 + k)(1 + s)), поэтому решается методом Гаусса–Ньютона.
Ковариация оценки (JᵀWJ + P)⁻¹ даёт погрешность каждого параметра — по ней решается,
стоит ли предлагать изменение.

Проверка: часть наблюдений (30 %) откладывается, параметры подбираются по остальным, и
сравнивается ошибка предсказания отложенных до и после. На сгенерированной сети
дополнительно моделируются повреждения и сравнивается точность локализации до и после.
"""
import math
import random
from dataclasses import dataclass, field
from datetime import UTC, datetime

import numpy as np
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Cable, CalibrationRun, CrossPort, Incident, IncidentStatus, Measurement
from app.services import incidents, tracing
from app.services.calib_state import RAW, current_state
from app.services.editing import EditError, audit
from app.services.localization import OTDR_ERR_BASE_M, OTDR_ERR_REL

DEFAULT_TWIST = 0.005
PRIOR_K_SD = 0.004       # априорная неопределённость укрутки (1σ)
PRIOR_S_SD = 0.10        # априорная неопределённость поправки карты (1σ)
TAU_START = 0.05         # начальная оценка разброса поправки карты (1σ)
# Нижняя граница разброса: даже после поправки длина «по карте» не точнее ±4 % (2σ) —
# оценка по остаткам при малом числе наблюдений бывает занижена до нуля
TAU_MIN = 0.02
# Оценка разброса по малому числу наблюдений ненадёжна (эксперимент этапа 12: при 3 повреждениях
# интервал занижался и истина попадала в него лишь в 70 % случаев). Поэтому τ «стягивается» к
# априорному значению (25 % — 2σ длины по карте) с весом, равным TAU_PRIOR_N наблюдениям.
TAU_PRIOR = 0.125
TAU_PRIOR_N = 8
INFORMATIVE = 0.75       # предлагать укрутку, если её σ уменьшилась хотя бы до 75 % априорной
MIN_OBS = 3


@dataclass
class Obs:
    kind: str                       # "measurement" | "incident"
    ref_id: int
    label: str
    d: float                        # измеренное оптическое расстояние, м
    known: dict[int, float] = field(default_factory=dict)   # марка кабеля → известная физическая длина
    geom: dict[int, float] = field(default_factory=dict)    # марка кабеля → длина «по карте»
    geom_sq: float = 0.0            # Σ (длина участка по карте)² — для вклада разброса τ
    var_fixed: float = 0.0          # дисперсия от известных длин и прибора


# ------------------------------------------------------------------ наблюдения


def _add(obs: Obs, e: tracing.Element, part: float, type_of: dict[int, int]) -> None:
    t = type_of[e.cable_id]
    if e.raw_geom_m is not None:
        g = part * e.raw_geom_m
        obs.geom[t] = obs.geom.get(t, 0.0) + g
        obs.geom_sq += g * g
    else:
        obs.known[t] = obs.known.get(t, 0.0) + part * e.length_m
        obs.var_fixed += (part * e.err2_m / 2) ** 2


def _otdr_var(d: float) -> float:
    return ((OTDR_ERR_BASE_M + OTDR_ERR_REL * d) / 2) ** 2


def observations(db: Session) -> tuple[list[Obs], list[str]]:
    """Все пригодные наблюдения и список причин, по которым часть данных пропущена."""
    result, skipped = [], []
    type_of = dict(db.execute(sa.select(Cable.id, Cable.cable_type_id)).all())
    for m in db.scalars(sa.select(Measurement).where(Measurement.optical_length_m.is_not(None),
                                                     Measurement.port_a_id.is_not(None),
                                                     Measurement.port_b_id.is_not(None))):
        pa, pb = db.get(CrossPort, m.port_a_id), db.get(CrossPort, m.port_b_id)
        if pa is None or pb is None or pa.fiber_id is None or pb.fiber_id is None:
            skipped.append(f"измерение №{m.id}: порт не подключён")
            continue
        tr = tracing.trace(db, pa.fiber_id, pa.fiber_end, start_port=pa, state=RAW)
        if tr.end_state != "port" or tr.legs[-1].fiber_id != pb.fiber_id:
            skipped.append(f"измерение №{m.id}: трасса от порта A не приходит в порт B — проверьте рассветку")
            continue
        o = Obs("measurement", m.id, f"измерение тракта №{m.id}", m.optical_length_m)
        for e in tr.elements:
            _add(o, e, 1.0, type_of)
        o.var_fixed += _otdr_var(o.d)
        result.append(o)
    for inc in db.scalars(sa.select(Incident).where(Incident.status == IncidentStatus.repaired,
                                                    Incident.actual_geom.is_not(None))):
        det = inc.calc_details or {}
        d = det.get("distance_corrected_m")
        if d is None:
            continue
        try:
            tr = incidents.trace_for(db, inc, state=RAW)
        except EditError as exc:
            skipped.append(f"повреждение №{inc.id}: {exc}")
            continue
        x, y = db.execute(sa.text("SELECT ST_X(actual_geom), ST_Y(actual_geom) FROM incident WHERE id = :i"),
                          {"i": inc.id}).one()
        _, off, idx, frac = incidents.project(tr, x, y, det.get("position_m"))
        if off > incidents.OFF_ROUTE_WARN_M:
            skipped.append(f"повреждение №{inc.id}: фактическое место в {off:.0f} м от трассы")
            continue
        o = Obs("incident", inc.id, f"повреждение №{inc.id}", float(d))
        for k, e in enumerate(tr.elements[: idx + 1]):
            _add(o, e, frac if k == idx else 1.0, type_of)
        o.var_fixed += _otdr_var(o.d)
        result.append(o)
    return result, skipped


# ------------------------------------------------------------------ модель и подбор


@dataclass
class Fit:
    s: float
    s_sd: float
    k: dict[int, float]      # марка кабеля → укрутка
    k_sd: dict[int, float]
    tau: float
    n_obs: dict[int, int]


def _prior_k(db: Session, type_ids: list[int]) -> dict[int, float]:
    """Априорная укрутка марки: среднее по её кабелям, кроме уже откалиброванных (их данные
    учитываются в самой калибровке — иначе одни и те же измерения вошли бы дважды)."""
    out = {t: DEFAULT_TWIST for t in type_ids}
    for t, avg in db.execute(sa.select(Cable.cable_type_id, sa.func.avg(Cable.twist_factor))
                             .where(Cable.cable_type_id.in_(type_ids),
                                    sa.or_(Cable.twist_source.is_(None), Cable.twist_source != "calibration"))
                             .group_by(Cable.cable_type_id)):
        out[t] = float(avg)
    return out


def predict(o: Obs, s: float, k: dict[int, float], k0: dict[int, float]) -> float:
    cables = set(o.known) | set(o.geom)
    return sum((1 + k.get(c, k0.get(c, DEFAULT_TWIST))) * (o.known.get(c, 0.0) + (1 + s) * o.geom.get(c, 0.0))
               for c in cables)


def fit(obs: list[Obs], k0: dict[int, float], iterations: int = 8) -> Fit:
    cables = sorted({c for o in obs for c in (set(o.known) | set(o.geom))})
    idx = {c: i + 1 for i, c in enumerate(cables)}
    theta0 = np.array([0.0] + [k0.get(c, DEFAULT_TWIST) for c in cables])
    prior_prec = np.diag([1 / PRIOR_S_SD ** 2] + [1 / PRIOR_K_SD ** 2] * len(cables))
    theta = theta0.copy()
    tau = TAU_START
    cov = np.linalg.inv(prior_prec)
    for outer in range(2):  # подбор параметров → оценка разброса τ → ещё раз подбор
        var = np.array([o.var_fixed + (tau ** 2) * o.geom_sq for o in obs])
        w = 1 / np.maximum(var, 1e-6)
        for _ in range(iterations):
            s = theta[0]
            r = np.zeros(len(obs))
            jac = np.zeros((len(obs), len(theta)))
            for j, o in enumerate(obs):
                f = 0.0
                for c in set(o.known) | set(o.geom):
                    kc = theta[idx[c]]
                    kn, gm = o.known.get(c, 0.0), o.geom.get(c, 0.0)
                    f += (1 + kc) * (kn + (1 + s) * gm)
                    jac[j, 0] += (1 + kc) * gm
                    jac[j, idx[c]] += kn + (1 + s) * gm
                r[j] = o.d - f
            a = jac.T @ (jac * w[:, None]) + prior_prec
            b = jac.T @ (w * r) - prior_prec @ (theta - theta0)
            delta = np.linalg.solve(a, b)
            theta = theta + delta
            cov = np.linalg.inv(a)
            if np.max(np.abs(delta)) < 1e-7:
                break
        if outer == 0 and any(o.geom_sq > 0 for o in obs):
            resid = np.array([o.d - predict(o, theta[0], {c: theta[idx[c]] for c in cables}, k0) for o in obs])
            excess = float(np.sum(resid ** 2 - np.array([o.var_fixed for o in obs])))
            tau = math.sqrt(max(excess, 0.0) / sum(o.geom_sq for o in obs)) if excess > 0 else TAU_MIN
            tau = min(max(tau, TAU_MIN), 0.15)
            n_map = sum(1 for o in obs if o.geom_sq > 0)
            tau = math.sqrt((n_map * tau ** 2 + TAU_PRIOR_N * TAU_PRIOR ** 2) / (n_map + TAU_PRIOR_N))
    sd = np.sqrt(np.diag(cov))
    n_obs = {c: sum(1 for o in obs if c in o.known or c in o.geom) for c in cables}
    return Fit(float(theta[0]), float(sd[0]), {c: float(theta[idx[c]]) for c in cables},
               {c: float(sd[idx[c]]) for c in cables}, tau, n_obs)


def _errors(obs: list[Obs], s: float, k: dict[int, float], k0: dict[int, float]) -> dict:
    if not obs:
        return {"n": 0}
    res = [o.d - predict(o, s, k, k0) for o in obs]
    return {"n": len(obs), "rmse_m": round(math.sqrt(sum(r * r for r in res) / len(res)), 2),
            "mae_m": round(sum(abs(r) for r in res) / len(res), 2),
            "bias_m": round(sum(res) / len(res), 2)}


# ------------------------------------------------------------------ прогон, применение, откат


def run(db: Session, *, holdout: float = 0.3, seed: int = 1, simulate: bool = True,
        user_id: int | None = None) -> CalibrationRun:
    obs, skipped = observations(db)
    if len(obs) < MIN_OBS:
        raise EditError(f"Мало данных для калибровки: наблюдений {len(obs)}, нужно хотя бы {MIN_OBS}. "
                        "Добавьте измерения трактов из ИД или отметьте фактические места повреждений.")
    types = sorted({t for o in obs for t in (set(o.known) | set(o.geom))})
    k0 = _prior_k(db, types)

    # 1. Проверка на отложенной выборке
    rnd = random.Random(seed)
    shuffled = obs[:]
    rnd.shuffle(shuffled)
    n_test = max(1, int(len(obs) * holdout)) if len(obs) >= 2 * MIN_OBS else 0
    test, train = shuffled[:n_test], shuffled[n_test:]
    f_train = fit(train, k0)
    holdout_res = {"before": _errors(test, 0.0, {}, k0), "after": _errors(test, f_train.s, f_train.k, k0)}

    # 2. Итоговая оценка по всем данным
    f = fit(obs, k0)
    marks = dict(db.execute(sa.text("SELECT id, mark FROM cable_type WHERE id = ANY(:ids)"), {"ids": types}).all())
    proposals = []
    for t in types:
        if f.k_sd[t] > INFORMATIVE * PRIOR_K_SD:
            continue  # данных по марке мало — значение почти не отличается от априорного
        # Укрутку, введённую вручную (по паспорту кабеля), калибровка не трогает
        cables = db.execute(sa.select(Cable.id, Cable.name, Cable.twist_factor, Cable.twist_err_2s,
                                      Cable.twist_source).where(
            Cable.cable_type_id == t, sa.or_(Cable.twist_source.is_(None), Cable.twist_source == "calibration"))
            .order_by(Cable.name)).all()
        proposals.append({"cable_type_id": t, "mark": marks[t], "prior": round(k0[t], 5),
                          "new": round(min(max(f.k[t], 0.0), 0.05), 5), "err_2s": round(2 * f.k_sd[t], 5),
                          "n_obs": f.n_obs[t],
                          "cables": [{"id": c, "name": n, "old": tw, "old_err": er, "old_source": src}
                                     for c, n, tw, er, src in cables]})
    has_map = any(o.geom_sq > 0 for o in obs)
    params = {
        "map_stretch": round(f.s, 4) if has_map else 0.0, "map_stretch_err_2s": round(2 * f.s_sd, 4),
        "geo_err_pct": round(min(max(200 * f.tau, 3.0), 25.0), 1) if has_map else None,
        "tau": round(f.tau, 4), "observations": len(obs),
        "by_kind": {k: sum(1 for o in obs if o.kind == k) for k in ("measurement", "incident")},
        "holdout": holdout_res, "skipped": skipped[:50], "seed": seed,
        "fit_all": {"before": _errors(obs, 0.0, {}, k0), "after": _errors(obs, f.s, f.k, k0)},
    }
    cr = CalibrationRun(params=params, proposals=proposals, metrics_before=holdout_res["before"],
                        metrics_after=holdout_res["after"])
    db.add(cr)
    db.flush()
    if simulate:
        _simulated_check(db, cr)
    audit(db, user_id, "calibrate", "calibration_run", cr.id, None,
          {"observations": len(obs), "marks": len(proposals), "map_stretch": params["map_stretch"]})
    return cr


def _simulated_check(db: Session, cr: CalibrationRun) -> None:
    """На сгенерированной сети: точность локализации до и после (применение — во временной точке сохранения)."""
    from app.services import experiment

    if not experiment.has_truth(db):
        return
    before = experiment.run_experiment(db, n=200, seed=99)
    nested = db.begin_nested()
    try:
        _apply_values(db, cr)
        cr.applied_at = datetime.now(UTC)  # временно — чтобы трассировка увидела новые параметры
        db.flush()
        after = experiment.run_experiment(db, n=200, seed=99)
    finally:
        nested.rollback()
    db.refresh(cr)
    params = dict(cr.params)
    params["simulation"] = {"before": before, "after": after}
    cr.params = params
    db.flush()


def _apply_values(db: Session, cr: CalibrationRun) -> None:
    for p in cr.proposals:
        for c in p["cables"]:
            cable = db.get(Cable, c["id"])
            if cable is not None:
                cable.twist_factor, cable.twist_err_2s, cable.twist_source = p["new"], p["err_2s"], "calibration"


def apply(db: Session, cr: CalibrationRun, user_id: int | None) -> CalibrationRun:
    if cr.applied_at is not None:
        raise EditError("Этот прогон уже применён")
    _apply_values(db, cr)
    cr.applied_at = datetime.now(UTC)
    cr.approved_by = user_id
    db.flush()
    audit(db, user_id, "apply", "calibration_run", cr.id, None,
          {"marks": len(cr.proposals), "cables": sum(len(p["cables"]) for p in cr.proposals),
           "map_stretch": cr.params.get("map_stretch")})
    return cr


def revert(db: Session, cr: CalibrationRun, user_id: int | None) -> CalibrationRun:
    if cr.applied_at is None or (cr.params or {}).get("reverted"):
        raise EditError("Прогон не применён или уже отменён")
    if current_state(db).run_id != cr.id:
        raise EditError("Отменить можно только последнюю применённую калибровку")
    for p in cr.proposals:
        for c in p["cables"]:
            cable = db.get(Cable, c["id"])
            if cable is not None and cable.twist_source == "calibration":
                cable.twist_factor = c["old"]
                cable.twist_err_2s, cable.twist_source = c.get("old_err"), c.get("old_source")
    params = dict(cr.params)
    params["reverted"] = True
    cr.params = params
    db.flush()
    audit(db, user_id, "revert", "calibration_run", cr.id, None, {"marks": len(cr.proposals)})
    return cr


def run_summary(cr: CalibrationRun) -> dict:
    p = cr.params or {}
    return {"id": cr.id, "created_at": cr.created_at.isoformat() if cr.created_at else None,
            "applied_at": cr.applied_at.isoformat() if cr.applied_at else None,
            "reverted": bool(p.get("reverted")), "params": p, "proposals": cr.proposals,
            "metrics_before": cr.metrics_before, "metrics_after": cr.metrics_after}
