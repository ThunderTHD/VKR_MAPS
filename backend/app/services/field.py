"""Работа в поле: что рядом со мной.

По координатам телефона (GPS) возвращаются ближайшие точки сети (колодцы, опоры, здания),
муфты и повреждения в работе — с расстоянием по прямой. Расстояние считается в PostGIS
по эллипсоиду (geography), отбор — по пространственному индексу (ST_DWithin), поэтому
запрос быстрый и на большой сети.
"""
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.services.cards import NODE_KIND_RU

NEARBY_SQL = """
WITH me AS (SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography AS g)
SELECT * FROM (
    SELECT 'node' AS type, n.id, n.kind::text AS kind, coalesce(n.code, n.name) AS label, n.address,
           ST_X(n.geom) AS lon, ST_Y(n.geom) AS lat, ST_Distance(n.geom::geography, me.g) AS dist,
           (SELECT count(*) FROM closure c WHERE c.node_id = n.id) AS closures
    FROM network_node n, me WHERE ST_DWithin(n.geom::geography, me.g, :r)
    UNION ALL
    SELECT 'closure', c.id, 'closure', 'Муфта ' || c.number, n.address,
           ST_X(n.geom), ST_Y(n.geom), ST_Distance(n.geom::geography, me.g), NULL
    FROM closure c JOIN network_node n ON n.id = c.node_id, me WHERE ST_DWithin(n.geom::geography, me.g, :r)
) x ORDER BY dist, type LIMIT :lim
"""

INCIDENTS_SQL = """
WITH me AS (SELECT ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography AS g)
SELECT i.id, i.title, i.status::text AS status, i.predicted_error_m AS half_width_m, i.detected_at,
       ST_X(i.predicted_geom) AS lon, ST_Y(i.predicted_geom) AS lat,
       ST_Distance(i.predicted_geom::geography, me.g) AS dist
FROM incident i, me
WHERE i.status IN ('open', 'localized') AND i.predicted_geom IS NOT NULL
ORDER BY dist LIMIT 20
"""


def nearby(db: Session, lon: float, lat: float, radius_m: float = 300, limit: int = 30) -> dict:
    rows = db.execute(sa.text(NEARBY_SQL), {"lon": lon, "lat": lat, "r": radius_m, "lim": limit}).mappings().all()
    objects = [{"type": r["type"], "id": r["id"], "kind": r["kind"],
                "kind_ru": "Муфта" if r["type"] == "closure" else NODE_KIND_RU.get(r["kind"], r["kind"]),
                "label": r["label"] or f"{NODE_KIND_RU.get(r['kind'], 'Точка')} #{r['id']}",
                "address": r["address"], "lonlat": [r["lon"], r["lat"]], "distance_m": round(r["dist"]),
                "closures": r["closures"]} for r in rows]
    incs = db.execute(sa.text(INCIDENTS_SQL), {"lon": lon, "lat": lat}).mappings().all()
    incidents = [{"id": r["id"], "title": r["title"], "status": r["status"], "half_width_m": r["half_width_m"],
                  "detected_at": r["detected_at"].isoformat() if r["detected_at"] else None,
                  "lonlat": [r["lon"], r["lat"]], "distance_m": round(r["dist"])} for r in incs]
    return {"objects": objects, "incidents": incidents, "radius_m": radius_m}
