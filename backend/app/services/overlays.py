"""Подложки: скан или PDF плана (ситуационный план, схема канализации), привязанный к карте.

Привязка — по опорным точкам: пользователь отмечает точку на картинке (пиксели) и ту же
точку на карте (широта/долгота), минимум три пары. По ним методом наименьших квадратов
строится аффинное преобразование «пиксели → метры» (сдвиг, поворот, масштаб по двум
осям, перекос) — его достаточно для скана плана, выполненного в одном масштабе. Расчёт —
в проекции Web Mercator (метры), в которой показывается и сама карта, поэтому картинка
ложится на подложку без искажений.

При четырёх и более точках считается невязка: насколько каждая опорная точка после
преобразования отстоит от отмеченной на карте. Большая невязка — ошибка в одной из точек
или неравномерно искажённый скан.

PDF переводится в картинку (первая или указанная страница, до 4000 точек по длинной
стороне) — браузер показывает на карте только растровые изображения.
"""
import io
import math

import numpy as np
from PIL import Image
from sqlalchemy.orm import Session

from app.models import Document, DocumentKind, MapOverlay
from app.services.editing import EditError, audit
from app.services.kml.jobs import store_upload

MAX_SIDE = 4000
R = 6378137.0


def merc(lon: float, lat: float) -> tuple[float, float]:
    return R * math.radians(lon), R * math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def unmerc(x: float, y: float) -> tuple[float, float]:
    return math.degrees(x / R), math.degrees(2 * math.atan(math.exp(y / R)) - math.pi / 2)


def _to_png(filename: str, data: bytes, page: int) -> tuple[bytes, int, int]:
    if data.startswith(b"%PDF-"):
        import pypdfium2 as pdfium
        try:
            pdf = pdfium.PdfDocument(data)
        except Exception as exc:  # noqa: BLE001
            raise EditError("PDF не открывается") from exc
        if not 1 <= page <= len(pdf):
            raise EditError(f"В PDF {len(pdf)} стр., страницы {page} нет")
        p = pdf[page - 1]
        w, h = p.get_size()
        img = p.render(scale=min(MAX_SIDE / max(w, h), 6)).to_pil()
    else:
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
        except Exception as exc:  # noqa: BLE001
            raise EditError("Нужна картинка (PNG, JPG) или PDF") from exc
        if max(img.size) > MAX_SIDE:
            img.thumbnail((MAX_SIDE, MAX_SIDE))
    img = img.convert("RGB")
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue(), img.width, img.height


def create(db: Session, *, filename: str, data: bytes, name: str | None, page: int = 1,
           user_id: int | None = None) -> MapOverlay:
    if len(data) > 60 * 1024 * 1024:
        raise EditError("Файл больше 60 МБ")
    png, w, h = _to_png(filename, data, page)
    base = filename.rsplit(".", 1)[0]
    doc = store_upload(db, f"{base}.png", png, DocumentKind.other, user_id)
    doc.mime, doc.note = "image/png", "подложка карты"
    ov = MapOverlay(name=(name or "").strip() or base, document_id=doc.id, opacity=0.6, visible=False,
                    control_points={"size": [w, h], "points": []})
    db.add(ov)
    db.flush()
    audit(db, user_id, "create", "map_overlay", ov.id, None, {"name": ov.name, "file": filename})
    return ov


def fit(points: list[dict], size: list[int]) -> dict:
    """Аффинное преобразование по опорным точкам [{px, py, lon, lat}] → углы картинки и невязки."""
    if len(points) < 3:
        raise EditError("Нужно хотя бы три опорные точки")
    src = np.array([[p["px"], p["py"], 1.0] for p in points])
    dst = np.array([merc(p["lon"], p["lat"]) for p in points])
    if np.linalg.matrix_rank(src) < 3:
        raise EditError("Опорные точки на картинке лежат на одной прямой — выберите точки в разных углах плана")
    coef, *_ = np.linalg.lstsq(src, dst, rcond=None)          # 3×2: x = a·px + b·py + c
    pred = src @ coef
    resid = np.sqrt(((pred - dst) ** 2).sum(axis=1))
    # Невязка в метрах на местности (Меркатор растягивает на 1/cos(широты))
    k = math.cos(math.radians(float(np.mean([p["lat"] for p in points]))))
    w, h = size

    def corner(px: float, py: float) -> list[float]:
        x, y = np.array([px, py, 1.0]) @ coef
        return list(unmerc(float(x), float(y)))
    # Масштаб: сколько метров в пикселе (по средней длине образов единичных векторов)
    scale = float((np.linalg.norm(coef[0]) + np.linalg.norm(coef[1])) / 2 * k)
    return {"corners": {"tl": corner(0, 0), "tr": corner(w, 0), "bl": corner(0, h), "br": corner(w, h)},
            "residuals_m": [round(float(r) * k, 2) for r in resid],
            "rms_m": round(float(np.sqrt(np.mean(resid ** 2))) * k, 2) if len(points) > 3 else None,
            "m_per_px": round(scale, 4)}


def set_points(db: Session, ov: MapOverlay, points: list[dict], user_id: int | None = None) -> dict:
    cp = dict(ov.control_points or {})
    res = fit(points, cp["size"])
    cp.update(points=points, **res)
    ov.control_points = cp
    ov.visible = True
    db.flush()
    audit(db, user_id, "georeference", "map_overlay", ov.id, None, {"points": len(points), "rms_m": res["rms_m"]})
    return res


def summary(ov: MapOverlay) -> dict:
    cp = ov.control_points or {}
    return {"id": ov.id, "name": ov.name, "opacity": ov.opacity, "visible": ov.visible,
            "url": f"/api/documents/{ov.document_id}/file" if ov.document_id else None,
            "size": cp.get("size"), "points": cp.get("points", []), "corners": cp.get("corners"),
            "rms_m": cp.get("rms_m"), "residuals_m": cp.get("residuals_m"), "m_per_px": cp.get("m_per_px")}


def update(db: Session, ov: MapOverlay, data: dict) -> None:
    if "name" in data and data["name"]:
        ov.name = data["name"].strip()
    if data.get("opacity") is not None:
        ov.opacity = min(max(float(data["opacity"]), 0.05), 1.0)
    if data.get("visible") is not None:
        if data["visible"] and not (ov.control_points or {}).get("corners"):
            raise EditError("Сначала привяжите подложку по опорным точкам")
        ov.visible = bool(data["visible"])
    db.flush()


def delete(db: Session, ov: MapOverlay, user_id: int | None = None) -> None:
    from app.services import documents
    doc = db.get(Document, ov.document_id) if ov.document_id else None
    audit(db, user_id, "delete", "map_overlay", ov.id, {"name": ov.name}, None)
    db.delete(ov)
    db.flush()
    if doc is not None:
        documents.delete_document(db, doc, user_id)
