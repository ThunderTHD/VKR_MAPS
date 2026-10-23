"""Перемычки: линия с подписью между двумя любыми объектами карты (ТЗ, пункт «Е»).

Перемычка — схематичная связь, которой нет в кабельной канализации: оптический патч-корд
между кроссами соседних помещений, резерв волокон между муфтами («Резерв 24 волокна»),
связь узла доступа с муфтой. Рисуется прямой линией между объектами и не участвует в
трассировке — это пометка для людей, а не путь света.

Концы — муфта, узел доступа, кросс или точка сети (колодец, опора, здание). При выборе
щелчком по карте берётся ближайший объект; если в одной точке и колодец, и муфта —
предпочтение муфте (она интереснее), затем узлу доступа и кроссу.
"""
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Jumper
from app.services.cards import NODE_KIND_RU
from app.services.editing import EditError, audit

TYPES = {"node", "closure", "cross", "access_node"}
TYPE_RU = {"node": "точка", "closure": "муфта", "cross": "кросс", "access_node": "узел доступа"}
PRIORITY = {"closure": 0, "access_node": 1, "cross": 2, "node": 3}

OBJECTS_SQL = """
    SELECT 'node' AS type, n.id, coalesce(n.code, n.name) AS label, n.kind::text AS kind, n.geom FROM network_node n
    UNION ALL SELECT 'closure', c.id, 'Муфта ' || c.number, NULL, n.geom
        FROM closure c JOIN network_node n ON n.id = c.node_id
    UNION ALL SELECT 'cross', x.id, x.name, NULL, n.geom FROM optical_cross x JOIN network_node n ON n.id = x.node_id
    UNION ALL SELECT 'access_node', a.id, a.hostname, NULL, n.geom
        FROM access_node a JOIN network_node n ON n.id = a.node_id
"""


def nearest(db: Session, lon: float, lat: float, within_m: float = 25.0) -> dict | None:
    rows = db.execute(sa.text(f"""
        SELECT type, id, label, kind, ST_X(geom) AS x, ST_Y(geom) AS y,
               ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) AS d
        FROM ({OBJECTS_SQL}) o
        WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :w)"""),
        {"lon": lon, "lat": lat, "w": within_m}).mappings().all()
    if not rows:
        return None
    # Сначала ближайшая точка на карте, а в одной точке — муфта важнее колодца
    r = min(rows, key=lambda r: (round(r["d"], 0), PRIORITY[r["type"]]))
    label = r["label"] or (NODE_KIND_RU.get(r["kind"], "точка") + f" #{r['id']}")
    return {"type": r["type"], "id": r["id"], "label": label, "type_ru": TYPE_RU[r["type"]],
            "lonlat": [r["x"], r["y"]], "distance_m": round(r["d"], 1)}


def _point(db: Session, kind: str, obj_id: int) -> tuple[float, float, str]:
    if kind not in TYPES:
        raise EditError("Конец перемычки: муфта, узел доступа, кросс или точка сети")
    r = db.execute(sa.text(f"SELECT ST_X(geom), ST_Y(geom), label FROM ({OBJECTS_SQL}) o "
                           "WHERE type = :t AND id = :i"), {"t": kind, "i": obj_id}).first()
    if r is None:
        raise EditError(f"{TYPE_RU[kind].capitalize()} #{obj_id} не найден(а)")
    return r[0], r[1], r[2] or f"#{obj_id}"


def create(db: Session, *, a_type: str, a_id: int, b_type: str, b_id: int, label: str | None,
           user_id: int | None = None) -> Jumper:
    if (a_type, a_id) == (b_type, b_id):
        raise EditError("Концы перемычки совпадают")
    ax, ay, _ = _point(db, a_type, a_id)
    bx, by, _ = _point(db, b_type, b_id)
    if (ax, ay) == (bx, by):
        bx += 1e-6       # объекты в одной точке (муфта и кросс в одном узле) — чтобы линия была видна
    j = Jumper(a_type=a_type, a_id=a_id, b_type=b_type, b_id=b_id, label=(label or "").strip() or None,
               geom=f"SRID=4326;LINESTRING({ax} {ay}, {bx} {by})")
    db.add(j)
    db.flush()
    audit(db, user_id, "create", "jumper", j.id, None, {"a": f"{a_type}:{a_id}", "b": f"{b_type}:{b_id}",
                                                         "label": j.label})
    return j


def update(db: Session, j: Jumper, label: str | None, user_id: int | None = None) -> None:
    before = {"label": j.label}
    j.label = (label or "").strip() or None
    db.flush()
    audit(db, user_id, "update", "jumper", j.id, before, {"label": j.label})


def delete(db: Session, j: Jumper, user_id: int | None = None) -> None:
    audit(db, user_id, "delete", "jumper", j.id, {"label": j.label, "a": f"{j.a_type}:{j.a_id}",
                                                  "b": f"{j.b_type}:{j.b_id}"}, None)
    db.delete(j)
    db.flush()


def card(db: Session, jumper_id: int) -> dict | None:
    j = db.get(Jumper, jumper_id)
    if j is None:
        return None
    ends = []
    for t, i in ((j.a_type, j.a_id), (j.b_type, j.b_id)):
        try:
            _, _, label = _point(db, t, i)
        except EditError:
            label = "объект удалён"
        ends.append({"type": t, "id": i, "label": f"{TYPE_RU.get(t, t).capitalize()}: {label}"})
    length = db.scalar(sa.text("SELECT ST_Length(geom::geography) FROM jumper WHERE id = :i"), {"i": j.id})
    mid = db.execute(sa.text("SELECT ST_X(p), ST_Y(p) FROM (SELECT ST_LineInterpolatePoint(geom, 0.5) p "
                             "FROM jumper WHERE id = :i) q"), {"i": j.id}).one()
    return {"type": "jumper", "id": j.id, "title": j.label or "Перемычка", "subtitle": "Перемычка",
            "lonlat": [mid[0], mid[1]],
            "fields": [["Подпись", j.label], ["Длина по прямой", f"{length:.0f} м"]],
            "edit": {"label": j.label},
            "sections": [{"title": "Концы", "items": ends}]}
