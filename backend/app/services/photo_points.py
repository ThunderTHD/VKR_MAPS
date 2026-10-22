"""Точки сети по фотографиям: пачка фото с координатами → привязка к точкам или новые точки.

Сценарий монтажника: идёт по трассе и фотографирует колодцы/опоры (камера с геотегом
или «GPS-камера»). Загружает пачку снимков; система для каждого находит координаты
(photo_coords) и ближайшую точку сети. Пользователь подтверждает: прикрепить фото к
найденной точке, создать новую точку (колодец, опору…) или пропустить.

Два шага, как при импорте KML: разбор ничего не меняет в сети (фото сохраняются без
владельца), запись — только после подтверждения. Неподтверждённые за сутки снимки удаляются.
"""
from concurrent.futures import ThreadPoolExecutor

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import Document, DocumentKind, NodeKind
from app.services import documents, drawing, photo_coords
from app.services.cards import NODE_KIND_RU
from app.services.editing import EditError, audit
from app.services.kml.jobs import store_upload

ATTACH_M = 25          # ближе этого — скорее всего та же точка (точность GPS телефона 5–15 м)
PENDING_NOTE = "импорт точек по фото — ожидает подтверждения"
MAX_FILES = 60


def _cleanup(db: Session) -> None:
    old = db.scalars(sa.select(Document).where(
        Document.owner_type.is_(None), Document.note == PENDING_NOTE,
        Document.created_at < sa.func.now() - sa.text("interval '1 day'"))).all()
    for d in old:
        (get_settings().upload_dir / d.path).unlink(missing_ok=True)
        db.delete(d)


def _nearest(db: Session, lon: float, lat: float, within_m: float = 200) -> dict | None:
    r = db.execute(sa.text("""
        SELECT id, kind, coalesce(code, name) AS label,
               ST_Distance(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography) AS d
        FROM network_node
        WHERE ST_DWithin(geom::geography, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326)::geography, :w)
        ORDER BY geom <-> ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) LIMIT 1"""),
        {"lon": lon, "lat": lat, "w": within_m}).mappings().first()
    if r is None:
        return None
    kind = str(r["kind"])
    return {"id": r["id"], "kind": kind, "kind_ru": NODE_KIND_RU.get(kind, kind),
            "label": r["label"] or f"{NODE_KIND_RU.get(kind, 'Точка')} #{r['id']}", "distance_m": round(r["d"], 1)}


def analyze(db: Session, files: list[tuple[str, bytes]], user_id: int | None) -> list[dict]:
    if not files:
        raise EditError("Не выбрано ни одного фото")
    if len(files) > MAX_FILES:
        raise EditError(f"За раз можно загрузить до {MAX_FILES} фото")
    _cleanup(db)
    for name, data in files:
        if documents._ext(name) not in photo_coords.IMAGE_EXT:
            raise EditError(f"«{name}» — не фото (нужны JPG/PNG/WEBP)")
        if len(data) > documents.MAX_BYTES:
            raise EditError(f"«{name}» больше 50 МБ")
    # Распознавание подписи (OCR) — самое долгое: параллельно в несколько потоков
    with ThreadPoolExecutor(4) as pool:
        metas = list(pool.map(lambda f: photo_coords.extract(f[1], f[0]), files))
    out = []
    for (name, data), meta in zip(files, metas, strict=True):
        doc = store_upload(db, name, data, DocumentKind.photo, user_id)
        doc.mime = {".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png",
                    ".webp": "image/webp"}.get(documents._ext(name), "application/octet-stream")
        doc.note = PENDING_NOTE
        item = {"doc_id": doc.id, "filename": name, "coords": None, "nearest": None, "suggest": "skip"}
        if meta and "lat" in meta:
            doc.geom = f"SRID=4326;POINT({meta['lon']} {meta['lat']})"
            doc.coord_source = meta["source"]
            item["coords"] = {"lat": meta["lat"], "lon": meta["lon"], "source": meta["source"]}
            near = _nearest(db, meta["lon"], meta["lat"])
            item["nearest"] = near
            item["suggest"] = "attach" if near and near["distance_m"] <= ATTACH_M else "create"
        if meta and meta.get("taken_at"):
            doc.taken_at = meta["taken_at"]
            item["taken_at"] = meta["taken_at"].isoformat()
        out.append(item)
    db.flush()
    return out


def commit(db: Session, items: list[dict], user_id: int | None) -> dict:
    """items: {doc_id, action: attach|create|skip, node_id?, kind?, code?}."""
    res = {"attached": 0, "created": 0, "skipped": 0, "nodes": []}
    for it in items:
        doc = db.get(Document, int(it["doc_id"]))
        if doc is None or doc.owner_type is not None or doc.note != PENDING_NOTE:
            raise EditError(f"Фото #{it['doc_id']} не найдено среди загруженных для импорта")
        action = it.get("action")
        if action == "skip":
            path = get_settings().upload_dir / doc.path
            db.delete(doc)
            db.flush()
            path.unlink(missing_ok=True)
            res["skipped"] += 1
            continue
        if action == "attach":
            node_id = int(it["node_id"]) if it.get("node_id") else None
            if node_id is None:
                raise EditError(f"«{doc.filename}»: не выбрана точка для прикрепления")
        elif action == "create":
            if doc.geom is None:
                raise EditError(f"«{doc.filename}»: у фото нет координат — новую точку не поставить")
            lon, lat = db.execute(sa.text("SELECT ST_X(geom), ST_Y(geom) FROM document WHERE id = :i"),
                                  {"i": doc.id}).one()
            kind = it.get("kind") or "well"
            if kind not in NodeKind.__members__:
                raise EditError(f"Неизвестный тип точки: {kind}")
            # Несколько снимков одного колодца с разных сторон — одна точка, а не несколько
            node_id = drawing.nearest_node(db, lon, lat, drawing.SNAP_M)
            if node_id is None:
                node = drawing.create_node(db, lon=lon, lat=lat, kind=kind, code=it.get("code"),
                                           note="поставлена по фото", user_id=user_id)
                node_id = node.id
                res["created"] += 1
                res["nodes"].append(node_id)
        else:
            raise EditError(f"Неизвестное действие: {action}")
        doc.owner_type, doc.owner_id, doc.note = "node", node_id, None
        audit(db, user_id, "attach", "node", node_id, None, {"document_id": doc.id, "filename": doc.filename})
        res["attached"] += 1
    db.flush()
    return res
