"""Экспорт сети: KML (Google Earth) и GeoJSON (QGIS, другие ГИС).

KML разбит на папки по типам — «Колодцы», «Опоры», «Трасса», «Кабели …» — так файл
удобно смотреть в Google Earth и можно загрузить обратно через импорт KML (этап 4):
названия папок совпадают с правилами сопоставления по умолчанию. Кабели раскрашены
цветом оператора. Фильтр по оператору — чтобы передать подрядчику только «его» часть.
"""
import json
from xml.sax.saxutils import escape

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.services.cards import NODE_KIND_RU, SEGMENT_KIND_RU

FOLDERS = {"well": "Колодцы", "kks": "ККС", "pole": "Опоры", "site": "Узлы связи", "building": "Здания",
           "closure_point": "Муфты вне колодцев", "unknown": "Прочие точки"}

NODES_SQL = """
    SELECT n.id, n.kind, n.code, n.name, n.address, ST_X(n.geom) AS x, ST_Y(n.geom) AS y
    FROM network_node n
    WHERE CAST(:op AS int) IS NULL OR EXISTS (
        SELECT 1 FROM cable_route cr JOIN cable c ON c.id = cr.cable_id JOIN route_segment s ON s.id = cr.segment_id
        WHERE c.operator_id = :op AND n.id IN (s.node_a_id, s.node_b_id))
    ORDER BY n.kind, n.code NULLS LAST, n.id"""
SEGMENTS_SQL = """
    SELECT s.id, s.kind, s.fact_length_m, ST_Length(s.geom::geography) AS geo_m,
           coalesce(na.code, na.name) AS a, coalesce(nb.code, nb.name) AS b, ST_AsGeoJSON(s.geom, 7)::json AS gj
    FROM route_segment s JOIN network_node na ON na.id = s.node_a_id JOIN network_node nb ON nb.id = s.node_b_id
    WHERE CAST(:op AS int) IS NULL OR EXISTS (
        SELECT 1 FROM cable_route cr JOIN cable c ON c.id = cr.cable_id
        WHERE cr.segment_id = s.id AND c.operator_id = :op)
    ORDER BY s.id"""
CABLES_SQL = """
    SELECT c.id, c.name, (SELECT count(*) FROM fiber f WHERE f.cable_id = c.id) AS fiber_count,
           o.short_name AS operator, coalesce(o.color, '#2c3e50') AS color,
           ct.mark, ST_AsGeoJSON(c.geom, 7)::json AS gj
    FROM cable c LEFT JOIN operator o ON o.id = c.operator_id LEFT JOIN cable_type ct ON ct.id = c.cable_type_id
    WHERE c.geom IS NOT NULL AND (CAST(:op AS int) IS NULL OR c.operator_id = :op)
    ORDER BY o.short_name NULLS LAST, c.name"""


def _rows(db: Session, sql: str, op: int | None):
    return db.execute(sa.text(sql), {"op": op}).mappings().all()


def _kml_color(hex_rgb: str, alpha: str = "ff") -> str:
    """KML пишет цвет как aabbggrr."""
    h = (hex_rgb or "#2c3e50").lstrip("#").ljust(6, "0")[:6]
    return f"{alpha}{h[4:6]}{h[2:4]}{h[0:2]}"


def _coords(gj: dict) -> str:
    return " ".join(f"{x:.7f},{y:.7f},0" for x, y in gj["coordinates"])


def _data(fields: dict) -> str:
    items = "".join(f'<Data name="{escape(k)}"><value>{escape(str(v))}</value></Data>'
                    for k, v in fields.items() if v not in (None, ""))
    return f"<ExtendedData>{items}</ExtendedData>" if items else ""


def to_kml(db: Session, operator_id: int | None = None, title: str = "ГИС ВОЛС") -> bytes:
    nodes, segs, cables = (_rows(db, q, operator_id) for q in (NODES_SQL, SEGMENTS_SQL, CABLES_SQL))
    out = ['<?xml version="1.0" encoding="UTF-8"?>', '<kml xmlns="http://www.opengis.net/kml/2.2"><Document>',
           f"<name>{escape(title)}</name>",
           '<Style id="seg"><LineStyle><color>ff9a9a9a</color><width>2</width></LineStyle></Style>']
    colors = {c["color"] for c in cables}
    for col in colors:
        out.append(f'<Style id="c{col.lstrip("#")}"><LineStyle><color>{_kml_color(col)}</color>'
                   f"<width>3</width></LineStyle></Style>")
    by_kind: dict[str, list] = {}
    for n in nodes:
        by_kind.setdefault(str(n["kind"]), []).append(n)
    for kind, items in by_kind.items():
        out.append(f"<Folder><name>{escape(FOLDERS.get(kind, kind))}</name>")
        for n in items:
            name = n["code"] or n["name"] or f"{NODE_KIND_RU.get(kind, 'Точка')} {n['id']}"
            out.append(f"<Placemark><name>{escape(name)}</name>"
                       + _data({"Тип": NODE_KIND_RU.get(kind, kind), "Название": n["name"], "Адрес": n["address"],
                                "id": n["id"]})
                       + f"<Point><coordinates>{n['x']:.7f},{n['y']:.7f},0</coordinates></Point></Placemark>")
        out.append("</Folder>")
    out.append("<Folder><name>Трасса</name>")
    for s in segs:
        kind = str(s["kind"])
        out.append(f"<Placemark><name>{escape(f'{s['a']} – {s['b']}')}</name><styleUrl>#seg</styleUrl>"
                   + _data({"Тип": SEGMENT_KIND_RU.get(kind, kind), "Длина факт, м": s["fact_length_m"],
                            "Длина по карте, м": round(s["geo_m"], 1), "id": s["id"]})
                   + f"<LineString><coordinates>{_coords(s['gj'])}</coordinates></LineString></Placemark>")
    out.append("</Folder>")
    by_op: dict[str, list] = {}
    for c in cables:
        by_op.setdefault(c["operator"] or "без оператора", []).append(c)
    for op, items in by_op.items():
        out.append(f"<Folder><name>{escape(f'Кабели {op}')}</name>")
        for c in items:
            out.append(f"<Placemark><name>{escape(c['name'])}</name><styleUrl>#c{c['color'].lstrip('#')}</styleUrl>"
                       + _data({"Марка": c["mark"], "Волокон": c["fiber_count"], "Оператор": c["operator"],
                                "id": c["id"]})
                       + f"<LineString><coordinates>{_coords(c['gj'])}</coordinates></LineString></Placemark>")
        out.append("</Folder>")
    out.append("</Document></kml>")
    return "\n".join(out).encode("utf-8")


def to_geojson(db: Session, operator_id: int | None = None) -> bytes:
    feats = []
    for n in _rows(db, NODES_SQL, operator_id):
        feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": [n["x"], n["y"]]},
                      "properties": {"layer": "node", "id": n["id"], "kind": str(n["kind"]), "code": n["code"],
                                     "name": n["name"], "address": n["address"]}})
    for s in _rows(db, SEGMENTS_SQL, operator_id):
        feats.append({"type": "Feature", "geometry": s["gj"],
                      "properties": {"layer": "segment", "id": s["id"], "kind": str(s["kind"]),
                                     "fact_length_m": s["fact_length_m"], "geo_length_m": round(s["geo_m"], 1),
                                     "from": s["a"], "to": s["b"]}})
    for c in _rows(db, CABLES_SQL, operator_id):
        feats.append({"type": "Feature", "geometry": c["gj"],
                      "properties": {"layer": "cable", "id": c["id"], "name": c["name"], "mark": c["mark"],
                                     "fiber_count": c["fiber_count"], "operator": c["operator"],
                                     "color": c["color"]}})
    fc = {"type": "FeatureCollection", "name": "vols", "features": feats}
    return json.dumps(fc, ensure_ascii=False).encode("utf-8")
