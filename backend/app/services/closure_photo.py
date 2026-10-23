"""Муфта по фотографии: монтажник фотографирует муфту — система ставит её на карту.

Координаты съёмки — из EXIF или с подписи «GPS-камеры» на кадре (photo_coords). Дальше
выбирается место муфты с учётом точности GPS (по подписи «Точность: 10.97 м» или 10 м
по умолчанию):
  1. точка сети рядом (колодец, опора, здание…) — муфты почти всегда стоят в колодце или на
     опоре, поэтому ближайшая точка в радиусе 1,5 × точности (10–30 м) — самый вероятный вариант;
  2. иначе участок трассы рядом (2 × точности, 15–40 м) — в участок вставляется новая точка
     «муфта вне колодца» в ближайшем к месту съёмки месте, кабели участка проходят через неё;
  3. иначе — отдельная точка там, где снято фото, с пометкой «рядом нет линий — нужно
     уточнить место и завести кабели».
Фото прикрепляется к муфте. Всё в одной транзакции: если что-то не так, ничего не создаётся.
"""
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Closure, DocumentKind, NetworkNode, NodeKind, RouteSegment
from app.services import drawing, photo_coords, splicing
from app.services.editing import EditError, audit
from app.services.kml.jobs import store_upload

DEFAULT_ACCURACY_M = 10.0
MAX_BYTES = 50 * 1024 * 1024
WHERE_RU = {"well": "в колодце", "kks": "в ККС", "pole": "на опоре", "building": "в здании", "site": "в узле связи",
            "closure_point": "в точке"}
NO_LINES_NOTE = "Поставлена по фото: рядом нет линий связи — уточните место и заведите кабели"


def _radii(accuracy: float | None) -> tuple[float, float]:
    acc = accuracy or DEFAULT_ACCURACY_M
    return max(10.0, min(1.5 * acc, 30.0)), max(15.0, min(2 * acc, 40.0))


def _nearest_node(db: Session, lon: float, lat: float, within: float):
    return db.execute(sa.text("""
        SELECT id, kind, coalesce(code, name) AS label,
               ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) AS d
        FROM network_node
        WHERE kind <> 'unknown'
          AND ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :w)
        ORDER BY d LIMIT 1"""), {"lon": lon, "lat": lat, "w": within}).mappings().first()


def _nearest_segment(db: Session, lon: float, lat: float, within: float):
    return db.execute(sa.text("""
        SELECT s.id, ST_Distance(s.geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) AS d,
               ST_X(ST_ClosestPoint(s.geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))) AS x,
               ST_Y(ST_ClosestPoint(s.geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))) AS y,
               ST_LineLocatePoint(s.geom, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)) AS frac,
               s.node_a_id, s.node_b_id
        FROM route_segment s
        WHERE ST_DWithin(s.geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :w)
        ORDER BY d LIMIT 1"""), {"lon": lon, "lat": lat, "w": within}).mappings().first()


def _operator_at(db: Session, node_id: int) -> int | None:
    """Оператор кабелей в точке, если он один, — муфта, скорее всего, его."""
    ops = db.scalars(sa.text("""
        SELECT DISTINCT c.operator_id FROM cable c JOIN cable_route cr ON cr.cable_id = c.id
        JOIN route_segment s ON s.id = cr.segment_id
        WHERE :n IN (s.node_a_id, s.node_b_id) AND c.operator_id IS NOT NULL"""), {"n": node_id}).all()
    return ops[0] if len(ops) == 1 else None


def place(db: Session, *, filename: str, data: bytes, number: str | None = None, model: str | None = None,
          user_id: int | None = None) -> dict:
    if len(data) > MAX_BYTES:
        raise EditError("Фото больше 50 МБ")
    meta = photo_coords.extract(data, filename)
    if not meta or "lat" not in meta:
        raise EditError("На фото не найдены координаты: ни в данных снимка (EXIF), ни в подписи на кадре. "
                        "Включите в камере «Сохранять местоположение» или снимайте приложением, которое "
                        "впечатывает координаты (NoteCam, GPS Map Camera)")
    lon, lat, acc = meta["lon"], meta["lat"], meta.get("accuracy_m")
    r_node, r_seg = _radii(acc)
    result = {"lat": lat, "lon": lon, "source": meta["source"], "accuracy_m": acc, "needs_data": False}
    node_row = _nearest_node(db, lon, lat, r_node)
    note = None
    if node_row is not None:
        node = db.get(NetworkNode, node_row["id"])
        where = WHERE_RU.get(str(node_row["kind"]), "в точке")
        label = f" {node_row['label']}" if node_row["label"] else ""
        result.update(mode="node", distance_m=round(node_row["d"], 1),
                      message=f"Муфта поставлена {where}{label} — {round(node_row['d'])} м от места съёмки")
    else:
        seg_row = _nearest_segment(db, lon, lat, r_seg)
        if seg_row is not None:
            seg = db.get(RouteSegment, seg_row["id"])
            if seg_row["frac"] <= 0.02 or seg_row["frac"] >= 0.98:     # у самого конца участка — его точка
                node = db.get(NetworkNode, seg.node_a_id if seg_row["frac"] < 0.5 else seg.node_b_id)
            else:
                node = drawing.split_segment(db, seg, seg_row["x"], seg_row["y"], NodeKind.closure_point.value,
                                             user_id)
            result.update(mode="segment", distance_m=round(seg_row["d"], 1),
                          message=f"Муфта поставлена на трассу — {round(seg_row['d'])} м от места съёмки")
        else:
            node = drawing.create_node(db, lon=lon, lat=lat, kind=NodeKind.closure_point.value,
                                       note=NO_LINES_NOTE, user_id=user_id, allow_near=True)
            note = NO_LINES_NOTE
            result.update(mode="free", distance_m=0.0, needs_data=True,
                          message=f"Рядом (до {round(r_seg)} м) нет линий связи — муфта поставлена в месте съёмки. "
                                  "Уточните место и заведите кабели")
    existing = db.scalar(sa.select(sa.func.count(Closure.id)).where(Closure.node_id == node.id))
    clo = splicing.create_closure(db, node_id=node.id, number=(number or "").strip() or f"М{existing + 1} (по фото)",
                                  model=model, operator_id=_operator_at(db, node.id), note=note, user_id=user_id)
    doc = store_upload(db, filename, data, DocumentKind.photo, user_id)
    doc.owner_type, doc.owner_id = "closure", clo.id
    doc.mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png", ".webp": "image/webp"}.get(
        filename[filename.rfind("."):].lower(), "application/octet-stream")
    doc.geom = f"SRID=4326;POINT({lon} {lat})"
    doc.coord_source = meta["source"]
    doc.taken_at = meta.get("taken_at")
    doc.note = f"фото муфты, точность GPS ± {acc:g} м" if acc else "фото муфты"
    db.flush()
    audit(db, user_id, "attach", "closure", clo.id, None, {"document_id": doc.id, "filename": filename})
    result.update(closure_id=clo.id, node_id=node.id, document_id=doc.id, number=clo.number)
    return result
