"""Данные для карты: слои в формате GeoJSON, построенные прямо в PostGIS.

GeoJSON собирается одним SQL-запросом (json_build_object + ST_AsGeoJSON), без
преобразования строк в Python-объекты и обратно: это быстрее и проще.

Фильтры:
- operator_id — объекты, которые использует оператор: кабели оператора, участки и узлы,
  через которые они проходят, его муфты, кроссы и узлы доступа;
- owner_id — инфраструктура владельца: его участки трасс и узлы (свои или на концах его участков);
- bbox — только объекты в видимой области карты (minLon, minLat, maxLon, maxLat).
"""
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session


@dataclass
class MapFilter:
    operator_id: int | None = None
    owner_id: int | None = None
    bbox: tuple[float, float, float, float] | None = None

    def params(self) -> dict:
        b = self.bbox or (None, None, None, None)
        return {"op": self.operator_id, "owner": self.owner_id,
                "x1": b[0], "y1": b[1], "x2": b[2], "y2": b[3]}


# Условие «объект в видимой области»; если bbox не задан — всегда истина
def _bbox(col: str) -> str:
    return (f"(CAST(:x1 AS float8) IS NULL OR "
            f"ST_Intersects({col}, ST_MakeEnvelope(:x1, :y1, :x2, :y2, 4326)))")


OP = "CAST(:op AS int)"
OWNER = "CAST(:owner AS int)"

# Операторы, кабели которых проходят по участку
SEG_OPS = """
seg_ops AS (
    SELECT cr.segment_id, array_agg(DISTINCT c.operator_id) AS ops, count(DISTINCT c.id) AS cables
    FROM cable_route cr JOIN cable c ON c.id = cr.cable_id
    GROUP BY cr.segment_id
)"""

# Операторы, присутствующие в узле: кабели через узел, муфты, кроссы, узлы доступа
NODE_OPS = """
node_ops AS (
    SELECT node_id, array_agg(DISTINCT op) AS ops FROM (
        SELECT rs.node_a_id AS node_id, c.operator_id AS op
          FROM cable_route cr JOIN cable c ON c.id = cr.cable_id JOIN route_segment rs ON rs.id = cr.segment_id
        UNION ALL
        SELECT rs.node_b_id, c.operator_id
          FROM cable_route cr JOIN cable c ON c.id = cr.cable_id JOIN route_segment rs ON rs.id = cr.segment_id
        UNION ALL SELECT node_id, operator_id FROM closure WHERE operator_id IS NOT NULL
        UNION ALL SELECT node_id, operator_id FROM optical_cross WHERE operator_id IS NOT NULL
        UNION ALL SELECT node_id, operator_id FROM access_node WHERE node_id IS NOT NULL AND operator_id IS NOT NULL
    ) x GROUP BY node_id
),
node_owners AS (
    SELECT node_id, array_agg(DISTINCT owner_id) AS owners FROM (
        SELECT node_a_id AS node_id, owner_id FROM route_segment WHERE owner_id IS NOT NULL
        UNION ALL SELECT node_b_id, owner_id FROM route_segment WHERE owner_id IS NOT NULL
    ) x GROUP BY node_id
)"""


def _collection(select_features: str) -> str:
    """Обернуть выборку Feature-объектов в FeatureCollection."""
    return ("SELECT json_build_object('type', 'FeatureCollection', 'features', "
            f"coalesce(json_agg(f.feature), '[]'::json))::text FROM ({select_features}) f")


SEGMENTS_SQL = f"""
WITH {SEG_OPS}
""" + _collection(f"""
    SELECT json_build_object(
        'type', 'Feature', 'id', s.id,
        'geometry', ST_AsGeoJSON(s.geom, 7)::json,
        'properties', json_build_object(
            'id', s.id, 'kind', s.kind, 'name', s.name, 'owner_id', s.owner_id,
            'geom_length_m', round(s.geom_length_m::numeric, 1),
            'fact_length_m', s.fact_length_m, 'length_source', s.length_source,
            'cables', coalesce(so.cables, 0), 'operators', coalesce(so.ops, '{{}}'::int[])
        )) AS feature
    FROM route_segment s LEFT JOIN seg_ops so ON so.segment_id = s.id
    WHERE ({OP} IS NULL OR {OP} = ANY(so.ops))
      AND ({OWNER} IS NULL OR s.owner_id = {OWNER})
      AND {_bbox('s.geom')}
""")

NODES_SQL = f"""
WITH {NODE_OPS}
""" + _collection(f"""
    SELECT json_build_object(
        'type', 'Feature', 'id', n.id,
        'geometry', ST_AsGeoJSON(n.geom, 7)::json,
        'properties', json_build_object(
            'id', n.id, 'kind', n.kind, 'code', n.code, 'name', n.name, 'owner_id', n.owner_id,
            'address', n.address,
            'closures', (SELECT count(*) FROM closure c WHERE c.node_id = n.id),
            'operators', coalesce(no.ops, '{{}}'::int[])
        )) AS feature
    FROM network_node n
    LEFT JOIN node_ops no ON no.node_id = n.id
    LEFT JOIN node_owners nw ON nw.node_id = n.id
    WHERE ({OP} IS NULL OR {OP} = ANY(no.ops))
      AND ({OWNER} IS NULL OR n.owner_id = {OWNER} OR {OWNER} = ANY(nw.owners))
      AND {_bbox('n.geom')}
""")

CABLES_SQL = _collection(f"""
    SELECT json_build_object(
        'type', 'Feature', 'id', c.id,
        'geometry', ST_AsGeoJSON(c.geom, 7)::json,
        'properties', json_build_object(
            'id', c.id, 'name', c.name, 'operator_id', c.operator_id, 'type', ct.mark,
            'fibers', ct.modules * ct.fibers_per_module
        )) AS feature
    FROM cable c JOIN cable_type ct ON ct.id = c.cable_type_id
    WHERE c.geom IS NOT NULL
      AND ({OP} IS NULL OR c.operator_id = {OP})
      AND ({OWNER} IS NULL OR EXISTS (
            SELECT 1 FROM cable_route cr JOIN route_segment rs ON rs.id = cr.segment_id
            WHERE cr.cable_id = c.id AND rs.owner_id = {OWNER}))
      AND {_bbox('c.geom')}
""")

CLOSURES_SQL = _collection(f"""
    SELECT json_build_object(
        'type', 'Feature', 'id', cl.id,
        'geometry', ST_AsGeoJSON(n.geom, 7)::json,
        'properties', json_build_object(
            'id', cl.id, 'number', cl.number, 'node_id', n.id, 'node_code', n.code,
            'operator_id', cl.operator_id,
            'splices', (SELECT count(*) FROM splice sp WHERE sp.closure_id = cl.id)
        )) AS feature
    FROM closure cl JOIN network_node n ON n.id = cl.node_id
    WHERE ({OP} IS NULL OR cl.operator_id = {OP})
      AND ({OWNER} IS NULL OR n.owner_id = {OWNER} OR EXISTS (
            SELECT 1 FROM route_segment rs
            WHERE rs.owner_id = {OWNER} AND n.id IN (rs.node_a_id, rs.node_b_id)))
      AND {_bbox('n.geom')}
""")

ACCESS_SQL = _collection(f"""
    SELECT json_build_object(
        'type', 'Feature', 'id', a.id,
        'geometry', ST_AsGeoJSON(n.geom, 7)::json,
        'properties', json_build_object(
            'id', a.id, 'hostname', a.hostname, 'ip', host(a.ip), 'status', a.status,
            'operator_id', a.operator_id, 'node_id', n.id, 'address', coalesce(a.address, n.address)
        )) AS feature
    FROM access_node a JOIN network_node n ON n.id = a.node_id
    WHERE ({OP} IS NULL OR a.operator_id = {OP})
      AND {_bbox('n.geom')}
""")

JUMPERS_SQL = _collection(f"""
    SELECT json_build_object(
        'type', 'Feature', 'id', j.id,
        'geometry', ST_AsGeoJSON(j.geom, 7)::json,
        'properties', json_build_object('id', j.id, 'label', j.label,
                                        'a_type', j.a_type, 'a_id', j.a_id, 'b_type', j.b_type, 'b_id', j.b_id)
    ) AS feature
    FROM jumper j WHERE {_bbox('j.geom')}
""")

UNCLASSIFIED_SQL = _collection(f"""
    SELECT json_build_object(
        'type', 'Feature', 'id', u.id,
        'geometry', ST_AsGeoJSON(u.geom, 7)::json,
        'properties', json_build_object('id', u.id, 'name', u.name, 'folder', u.folder)
    ) AS feature
    FROM unclassified_feature u WHERE {_bbox('u.geom')}
""")

INCIDENTS_SQL = _collection(f"""
    SELECT json_build_object(
        'type', 'Feature', 'id', i.id,
        'geometry', ST_AsGeoJSON(i.predicted_geom, 7)::json,
        'properties', json_build_object(
            'id', i.id, 'title', i.title, 'status', i.status, 'half_width_m', i.predicted_error_m,
            'error_m', i.error_m, 'simulated', i.source_kind = 'generator',
            'actual', CASE WHEN i.actual_geom IS NULL THEN NULL ELSE ST_AsGeoJSON(i.actual_geom, 7)::json END,
            'detected_at', i.detected_at
        )) AS feature
    FROM incident i
    WHERE i.predicted_geom IS NOT NULL AND i.status <> 'cancelled' AND {_bbox('i.predicted_geom')}
""")

LAYERS = {
    "segments": SEGMENTS_SQL,
    "nodes": NODES_SQL,
    "cables": CABLES_SQL,
    "closures": CLOSURES_SQL,
    "access_nodes": ACCESS_SQL,
    "jumpers": JUMPERS_SQL,
    "unclassified": UNCLASSIFIED_SQL,
    "incidents": INCIDENTS_SQL,
}


def layer_geojson(db: Session, layer: str, flt: MapFilter) -> str:
    """GeoJSON слоя в виде готовой строки (отдаётся клиенту без пересборки)."""
    return db.execute(sa.text(LAYERS[layer]), flt.params()).scalar_one()


def network_extent(db: Session) -> list[float] | None:
    """Границы всей сети [minLon, minLat, maxLon, maxLat] — чтобы карта открывалась «на сети»."""
    row = db.execute(sa.text(
        "SELECT ST_XMin(e), ST_YMin(e), ST_XMax(e), ST_YMax(e) FROM "
        "(SELECT ST_Extent(geom) AS e FROM network_node) x")).one()
    return None if row[0] is None else [float(v) for v in row]
