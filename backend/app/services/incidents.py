"""Журнал повреждений: прогноз места, фактическое место после ремонта, ошибка прогноза.

Фактическое место, отмеченное на карте, проецируется на трассу волокна (ту же, по
которой делался прогноз). Так получаются две ошибки:
- по прямой — расстояние между точками на карте;
- по трассе — насколько метров вдоль кабеля ошибся прогноз; именно её сравнивают с
  интервалом погрешности и именно она служит наблюдением для калибровки (этап 8).
"""
import math
import statistics
from datetime import UTC, datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import CrossPort, Incident, IncidentStatus
from app.services import tracing
from app.services.editing import EditError, audit
from app.services.localization import _cum, _dist

STATUS_RU = {"open": "обнаружено", "localized": "место рассчитано", "repaired": "устранено",
             "cancelled": "отменено"}
OFF_ROUTE_WARN_M = 150


def trace_for(db: Session, inc: Incident, state=None) -> tracing.Trace:
    """Трасса, по которой считался прогноз повреждения."""
    start = (inc.calc_details or {}).get("start") or {}
    port_id = start.get("port_id") or inc.measure_port_id
    if port_id:
        port = db.get(CrossPort, port_id)
        if port is None or port.fiber_id is None:
            raise EditError("Порт измерения больше не подключён — трассу восстановить нельзя")
        return tracing.trace(db, port.fiber_id, port.fiber_end, start_port=port, state=state)
    if inc.fiber_id and start.get("end"):
        return tracing.trace(db, inc.fiber_id, start["end"], state=state)
    raise EditError("Не известна точка измерения повреждения")


def project(tr: tracing.Trace, lon: float, lat: float,
            near_pos: float | None = None) -> tuple[float, float, int, float]:
    """Ближайшая точка трассы: (положение по трассе, м; расстояние до трассы, м; индекс элемента; доля).

    Тракт может дважды проходить по одной канализации (ответвление идёт назад вдоль
    магистрали). Тогда точка одинаково близка к двум местам трассы; из таких кандидатов
    (в пределах 5 м от лучшего) берётся ближайший к near_pos — к прогнозу.
    """
    cands: list[tuple[float, float, int, float]] = []  # (расстояние, положение, элемент, доля)
    p = [lon, lat]
    for idx, e in enumerate(tr.elements):
        if e.kind != "segment" or len(e.coords) < 2:
            continue
        acc = _cum(e.coords)
        total = acc[-1] or 1.0
        for i in range(1, len(e.coords)):
            a, b = e.coords[i - 1], e.coords[i]
            seg = acc[i] - acc[i - 1]
            t = 0.0
            if seg > 0:
                # проекция в локальной метрике: перевод в метры относительно точки a
                k = math.cos(math.radians(a[1]))
                ax, ay = (b[0] - a[0]) * 111_320 * k, (b[1] - a[1]) * 110_574
                px, py = (p[0] - a[0]) * 111_320 * k, (p[1] - a[1]) * 110_574
                t = max(0.0, min(1.0, (px * ax + py * ay) / (ax * ax + ay * ay)))
            q = [a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t]
            f = (acc[i - 1] + t * seg) / total
            cands.append((_dist(p, q), e.start_phys + f * e.length_m, idx, f))
    if not cands:
        raise EditError("У трассы нет участков")
    best_d = min(c[0] for c in cands)
    close = [c for c in cands if c[0] <= best_d + 5]
    best = min(close, key=lambda c: abs(c[1] - near_pos)) if near_pos is not None else min(cands)
    return best[1], best[0], best[2], best[3]


def set_actual(db: Session, inc: Incident, lon: float, lat: float, *, restored_at: datetime | None = None,
               note: str | None = None, user_id: int | None = None) -> Incident:
    if inc.predicted_geom is None:
        raise EditError("У повреждения нет прогноза места")
    if inc.status == IncidentStatus.cancelled:
        raise EditError("Повреждение отменено")
    px, py = db.execute(sa.text("SELECT ST_X(predicted_geom), ST_Y(predicted_geom) FROM incident WHERE id = :i"),
                        {"i": inc.id}).one()
    tr = trace_for(db, inc)
    hint = (inc.calc_details or {}).get("position_m")
    pred_pos, _, _, _ = project(tr, px, py, hint)
    actual_pos, off_route, _, _ = project(tr, lon, lat, pred_pos)
    straight = db.execute(sa.text("""
        SELECT ST_Distance(predicted_geom::geography, ST_SetSRID(ST_MakePoint(:x, :y), 4326)::geography)
        FROM incident WHERE id = :i"""), {"x": lon, "y": lat, "i": inc.id}).scalar_one()
    route_err = abs(actual_pos - pred_pos)
    h = inc.predicted_error_m or 0.0
    before = {"status": inc.status.value, "error_m": inc.error_m}
    inc.actual_geom = f"SRID=4326;POINT({lon} {lat})"
    inc.error_m = round(straight, 1)
    inc.status = IncidentStatus.repaired
    inc.restored_at = restored_at or datetime.now(UTC)
    if note:
        inc.note = (inc.note + "\n" if inc.note else "") + note.strip()
    details = dict(inc.calc_details or {})
    details["actual"] = {"position_m": round(actual_pos, 1), "off_route_m": round(off_route, 1),
                         "route_error_m": round(route_err, 1), "within_interval": route_err <= h + 0.5}
    inc.calc_details = details
    db.flush()
    audit(db, user_id, "repair", "incident", inc.id, before,
          {"status": "repaired", "error_m": inc.error_m, "route_error_m": round(route_err, 1)})
    return inc


INCIDENT_FIELDS = ["title", "note"]


def update_incident(db: Session, inc: Incident, changes: dict, user_id: int | None) -> Incident:
    before = {f: getattr(inc, f) for f in INCIDENT_FIELDS}
    for k, v in changes.items():
        if k == "status":
            if v not in ("cancelled", "localized"):
                raise EditError("Статус «устранено» ставится отметкой фактического места")
            if v == "localized" and inc.actual_geom is not None:
                inc.actual_geom, inc.error_m, inc.restored_at = None, None, None
                details = dict(inc.calc_details or {})
                details.pop("actual", None)
                inc.calc_details = details
            inc.status = IncidentStatus(v)
        elif k in INCIDENT_FIELDS:
            setattr(inc, k, (v or "").strip() or None)
        else:
            raise EditError(f"Поле «{k}» менять нельзя")
    db.flush()
    audit(db, user_id, "update", "incident", inc.id, before,
          {**{f: getattr(inc, f) for f in INCIDENT_FIELDS}, "status": inc.status.value})
    return inc


def delete_incident(db: Session, inc: Incident, user_id: int | None) -> None:
    audit(db, user_id, "delete", "incident", inc.id, {"title": inc.title, "status": inc.status.value})
    db.delete(inc)
    db.flush()


def list_incidents(db: Session, status: str | None = None, limit: int = 500) -> list[dict]:
    rows = db.execute(sa.text("""
        SELECT i.id, i.title, i.status, i.detected_at, i.restored_at, i.otdr_distance_m, i.predicted_error_m,
               i.error_m, i.source_kind, ST_X(i.predicted_geom) AS lon, ST_Y(i.predicted_geom) AS lat,
               i.calc_details -> 'actual' AS actual, c.name AS cable, f.number AS fiber
        FROM incident i LEFT JOIN fiber f ON f.id = i.fiber_id LEFT JOIN cable c ON c.id = f.cable_id
        WHERE (CAST(:s AS text) IS NULL OR i.status::text = :s)
        ORDER BY coalesce(i.detected_at, i.created_at) DESC, i.id DESC LIMIT :lim"""),
        {"s": status, "lim": limit}).mappings().all()
    out = []
    for r in rows:
        a = r["actual"] or {}
        out.append({"id": r["id"], "title": r["title"], "status": r["status"], "status_ru": STATUS_RU[r["status"]],
                    "detected_at": r["detected_at"].isoformat() if r["detected_at"] else None,
                    "restored_at": r["restored_at"].isoformat() if r["restored_at"] else None,
                    "otdr_distance_m": r["otdr_distance_m"], "half_width_m": r["predicted_error_m"],
                    "error_m": r["error_m"], "route_error_m": a.get("route_error_m"),
                    "within_interval": a.get("within_interval"), "cable": r["cable"], "fiber": r["fiber"],
                    "lonlat": [r["lon"], r["lat"]] if r["lon"] is not None else None,
                    "simulated": r["source_kind"] == "generator"})
    return out


def stats(db: Session) -> dict:
    items = list_incidents(db, limit=100_000)
    by_status = {k: 0 for k in STATUS_RU}
    for i in items:
        by_status[i["status"]] += 1
    rep = [i for i in items if i["status"] == "repaired" and i["route_error_m"] is not None]
    if not rep:
        return {"by_status": by_status, "repaired": 0}
    errs = [i["route_error_m"] for i in rep]
    return {"by_status": by_status, "repaired": len(rep),
            "route_error_mean_m": round(statistics.mean(errs), 1),
            "route_error_median_m": round(statistics.median(errs), 1),
            "route_error_p90_m": round(sorted(errs)[int(0.9 * (len(errs) - 1))], 1),
            "half_width_mean_m": round(statistics.mean(i["half_width_m"] or 0 for i in rep), 1),
            "within_pct": round(100 * sum(1 for i in rep if i["within_interval"]) / len(rep), 1)}
