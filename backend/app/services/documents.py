"""Документы, прикреплённые к объектам сети: схемы, паспорта, акты, фото.

Файлы лежат в хранилище (UPLOAD_DIR), в базе — запись Document с полиморфной ссылкой
owner_type + owner_id. Типы владельцев совпадают с типами карточек интерфейса.

Безопасность: допускаются только перечисленные расширения; имя файла очищается от путей;
«в браузере» (inline) открываются лишь PDF и картинки, чьё содержимое действительно
начинается с подписи своего формата — иначе файл отдаётся на скачивание. Это не даёт
выдать HTML со скриптом за картинку и выполнить его на сайте системы.
"""
import csv
import io
import mimetypes

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import get_settings
from app.models import (
    AccessNode,
    Cable,
    Closure,
    Document,
    DocumentKind,
    Incident,
    NetworkNode,
    OpticalCross,
    RouteSegment,
)
from app.services import photo_coords
from app.services.editing import EditError, audit
from app.services.kml.jobs import store_upload

MAX_BYTES = 50 * 1024 * 1024
OWNERS = {"incident": Incident,
          "node": NetworkNode, "segment": RouteSegment, "cable": Cable, "closure": Closure, "cross": OpticalCross,
          "access_node": AccessNode}
ALLOWED_EXT = {".pdf", ".png", ".jpg", ".jpeg", ".gif", ".webp", ".heic", ".heif", ".doc", ".docx", ".xls", ".xlsx",
               ".odt", ".ods", ".txt", ".csv", ".sor", ".kml", ".kmz", ".dwg", ".dxf", ".zip", ".7z", ".rar"}
# Подписи форматов, которые можно показывать прямо в браузере
INLINE_MAGIC = {".pdf": [b"%PDF-"], ".png": [b"\x89PNG\r\n\x1a\n"], ".jpg": [b"\xff\xd8\xff"],
                ".jpeg": [b"\xff\xd8\xff"], ".gif": [b"GIF87a", b"GIF89a"], ".webp": [b"RIFF"]}
KIND_RU = {"splice_scheme": "Схема рассветки", "route_scheme": "Схема трассы", "passport": "Паспорт",
           "act": "Акт", "asbuilt": "Исполнительная документация", "calibration": "Свидетельство о поверке",
           "photo": "Фото", "other": "Прочее"}


def _ext(name: str) -> str:
    dot = name.rfind(".")
    return name[dot:].lower() if dot >= 0 else ""


def attach(db: Session, *, owner_type: str, owner_id: int, filename: str, data: bytes, kind: str = "other",
           note: str | None = None, user_id: int | None = None) -> Document:
    model = OWNERS.get(owner_type)
    if model is None or db.get(model, owner_id) is None:
        raise EditError("Объект для прикрепления не найден")
    if not data:
        raise EditError("Файл пустой")
    if len(data) > MAX_BYTES:
        raise EditError("Файл больше 50 МБ")
    if _ext(filename) not in ALLOWED_EXT:
        raise EditError(f"Тип файла {_ext(filename) or 'без расширения'} не поддерживается. Можно: "
                        + ", ".join(sorted(e[1:] for e in ALLOWED_EXT)))
    try:
        dk = DocumentKind(kind)
    except ValueError:
        raise EditError("Неизвестный вид документа") from None
    doc = store_upload(db, filename, data, dk, user_id)
    doc.owner_type, doc.owner_id = owner_type, owner_id
    doc.mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    doc.note = (note or "").strip() or None
    set_coords(doc, data, ocr=dk == DocumentKind.photo)
    db.flush()
    audit(db, user_id, "attach", owner_type, owner_id, None, {"document_id": doc.id, "filename": filename})
    return doc


def set_coords(doc: Document, data: bytes, *, ocr: bool = True) -> dict | None:
    """Координаты места съёмки (EXIF или подпись на кадре) — в запись документа."""
    meta = photo_coords.extract(data, doc.filename, ocr=ocr)
    if meta and "lat" in meta:
        doc.geom = f"SRID=4326;POINT({meta['lon']} {meta['lat']})"
        doc.coord_source = meta["source"]
    if meta and meta.get("taken_at"):
        doc.taken_at = meta["taken_at"]
    return meta


# Геометрия владельца документа — чтобы показать, далеко ли от объекта снято фото
OWNER_GEOM_SQL = """
    CASE d.owner_type
        WHEN 'node' THEN (SELECT geom FROM network_node WHERE id = d.owner_id)
        WHEN 'segment' THEN (SELECT geom FROM route_segment WHERE id = d.owner_id)
        WHEN 'cable' THEN (SELECT geom FROM cable WHERE id = d.owner_id)
        WHEN 'closure' THEN (SELECT n.geom FROM closure c JOIN network_node n ON n.id = c.node_id
                             WHERE c.id = d.owner_id)
        WHEN 'cross' THEN (SELECT n.geom FROM optical_cross x JOIN network_node n ON n.id = x.node_id
                           WHERE x.id = d.owner_id)
        WHEN 'access_node' THEN (SELECT n.geom FROM access_node a JOIN network_node n ON n.id = a.node_id
                                 WHERE a.id = d.owner_id)
        WHEN 'incident' THEN (SELECT coalesce(actual_geom, predicted_geom) FROM incident WHERE id = d.owner_id)
    END"""


def list_documents(db: Session, owner_type: str, owner_id: int) -> list[dict]:
    rows = db.execute(sa.text(f"""
        SELECT d.id, d.kind, d.filename, d.size, d.note, d.created_at, u.full_name, d.coord_source, d.taken_at,
               ST_X(d.geom) AS x, ST_Y(d.geom) AS y,
               ST_Distance(d.geom::geography, ({OWNER_GEOM_SQL})::geography) AS dist
        FROM document d LEFT JOIN app_user u ON u.id = d.uploaded_by
        WHERE d.owner_type = :t AND d.owner_id = :i ORDER BY d.created_at DESC, d.id DESC"""),
        {"t": owner_type, "i": owner_id}).all()
    return [{"id": r.id, "kind": r.kind, "kind_ru": KIND_RU.get(r.kind, r.kind), "filename": r.filename,
             "size": r.size, "note": r.note, "created_at": r.created_at.isoformat(), "user": r.full_name,
             "inline": _ext(r.filename) in INLINE_MAGIC,
             "lonlat": [r.x, r.y] if r.x is not None else None, "coord_source": r.coord_source,
             "taken_at": r.taken_at.isoformat() if r.taken_at else None,
             "distance_m": round(r.dist, 1) if r.dist is not None else None} for r in rows]


def open_document(doc: Document) -> tuple[bytes, str, bool]:
    """Содержимое, MIME-тип и можно ли показать в браузере (а не только скачать)."""
    data = (get_settings().upload_dir / doc.path).read_bytes()
    ext = _ext(doc.filename)
    inline = any(data.startswith(sig) for sig in INLINE_MAGIC.get(ext, []))
    mime = (doc.mime or "application/octet-stream") if inline else "application/octet-stream"
    return data, mime, inline


def delete_document(db: Session, doc: Document, user_id: int | None) -> None:
    path = get_settings().upload_dir / doc.path
    audit(db, user_id, "detach", doc.owner_type or "document", doc.owner_id, {"document_id": doc.id,
                                                                              "filename": doc.filename}, None)
    db.delete(doc)
    db.flush()
    path.unlink(missing_ok=True)


# ------------------------------------------------------------------ экспорт рассветки


def closure_csv(sheet: dict) -> bytes:
    """Рассветка муфты в CSV для Excel: разделитель «;», UTF-8 с BOM (иначе Excel путает кодировку)."""
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    c = sheet["closure"]
    w.writerow([f"Муфта {c['number']}", f"узел {c['node']['label']}", c["model"] or ""])
    w.writerow([])
    w.writerow(["Кабель A", "Модуль", "ОВ", "Цвет ОВ", "Кабель Б", "Модуль", "ОВ", "Цвет ОВ", "Потери, дБ",
                "Примечание"])
    for s in sheet["splices"]:
        a, b = s["a"], s["b"]
        w.writerow([a["cable"], f"{a['module_no']} ({a['module_color'] or '—'})", a["fiber"], a["color"] or "",
                    b["cable"], f"{b['module_no']} ({b['module_color'] or '—'})", b["fiber"], b["color"] or "",
                    "" if s["loss_db"] is None else f"{s['loss_db']:.2f}".replace(".", ","), s["note"] or ""])
    return ("﻿" + buf.getvalue()).encode("utf-8")
