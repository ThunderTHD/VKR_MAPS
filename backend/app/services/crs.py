"""Системы координат: местные (МСК), UTM и др. — пересчёт в WGS 84 и обратно средствами PostGIS.

Сеть хранится в WGS 84 (EPSG:4326) — в нём работают карты и GPS телефонов. Но проекты,
исполнительные съёмки и выгрузки 5Gis/AutoCAD часто в местной системе координат субъекта
(МСК-16 для Татарстана и т. п.) или в UTM. Параметры МСК официально не публикуются — их
нужно взять из геоданных организации; проще всего — из файла .prj, который выгружает
та же программа (5Gis, MapInfo, QGIS, AutoCAD Map) вместе со Shapefile.

Пересчёт делает PostGIS (библиотека PROJ внутри), поэтому определение системы можно
дать в любом виде, который понимает PROJ: WKT из .prj, строка «+proj=tmerc …», «EPSG:32639».

В российской геодезии X — север, Y — восток; в ГИС и Shapefile x — восток, y — север.
Здесь везде x = восток, y = север (как в файлах), а при показе пользователю подписываем
«север» и «восток», чтобы не путать.
"""
import re

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import CrsDef
from app.services.editing import EditError

WGS84 = "EPSG:4326"


def is_wgs84(definition: str | None) -> bool:
    """Географические координаты WGS 84 (пересчёт не нужен)."""
    if not definition:
        return False
    d = definition.upper().replace(" ", "").replace("_", "")
    if d in ("EPSG:4326", "WGS84"):
        return True
    return d.startswith("GEOGCS[") and "WGS1984" in d.replace("WGS84", "WGS1984") and "PROJCS" not in d


def is_geographic(definition: str) -> bool:
    d = definition.strip().upper()
    return d.startswith("GEOGCS[") or d.startswith("GEOGCRS[") or "+PROJ=LONGLAT" in d or "+PROJ=LATLONG" in d


def check(db: Session, definition: str) -> dict:
    """Проверить, что PROJ понимает определение: пересчитать точку туда-обратно."""
    definition = (definition or "").strip()
    if not definition:
        raise EditError("Пустое определение системы координат")
    try:
        with db.begin_nested():
            x, y = db.execute(sa.text("""
                SELECT ST_X(g), ST_Y(g) FROM ST_Transform(ST_SetSRID(ST_MakePoint(52.33, 55.67), 4326),
                                                          CAST(:d AS text)) g"""), {"d": definition}).one()
    except Exception as exc:  # noqa: BLE001 — сообщение PROJ пользователю ни к чему
        raise EditError("Определение системы координат не распознано (нужен WKT из файла .prj, строка proj "
                        "или код EPSG)") from exc
    return {"sample_wgs84": [52.33, 55.67], "sample_xy": [round(x, 3), round(y, 3)],
            "geographic": is_geographic(definition)}


def transform(db: Session, points: list[tuple[float, float]], src: str, dst: str = WGS84) -> list[tuple[float, float]]:
    """Пересчитать точки (x=восток/долгота, y=север/широта) из src в dst одним запросом."""
    if not points or src == dst:
        return list(points)
    xs, ys = [float(p[0]) for p in points], [float(p[1]) for p in points]
    try:
        with db.begin_nested():
            rows = db.execute(sa.text("""
                SELECT ST_X(g), ST_Y(g) FROM (
                    SELECT ord, ST_Transform(ST_MakePoint(x, y), CAST(:src AS text), CAST(:dst AS text)) g
                    FROM unnest(CAST(:xs AS float8[]), CAST(:ys AS float8[])) WITH ORDINALITY AS t(x, y, ord)) q
                ORDER BY ord"""), {"xs": xs, "ys": ys, "src": src, "dst": dst}).all()
    except Exception as exc:  # noqa: BLE001
        raise EditError("Не удалось пересчитать координаты: система координат не распознана") from exc
    return [(float(a), float(b)) for a, b in rows]


def to_wgs84(db: Session, points: list[tuple[float, float]], definition: str | None) -> list[tuple[float, float]]:
    """В WGS 84 с проверкой результата: точки должны оказаться на Земле, а не в «нулевом» месте."""
    if not points:
        return []
    if definition is None or is_wgs84(definition):
        if all(-180 <= x <= 180 and -90 <= y <= 90 for x, y in points):
            return list(points)
        raise EditError("Координаты в файле не похожи на широту/долготу (это метры?) — укажите систему "
                        "координат: приложите .prj или выберите систему в списке")
    out = transform(db, points, definition, WGS84)
    bad = sum(1 for x, y in out if not (-180 <= x <= 180 and -90 <= y <= 90) or x != x)
    if bad:
        raise EditError(f"После пересчёта {bad} точек оказались вне Земли — система координат указана неверно")
    return out


def list_crs(db: Session) -> list[dict]:
    return [{"id": c.id, "name": c.name, "definition": c.definition, "show_in_cards": c.show_in_cards,
             "geographic": is_geographic(c.definition)}
            for c in db.scalars(sa.select(CrsDef).order_by(CrsDef.name))]


def name_from_prj(prj: str) -> str | None:
    m = re.match(r'\s*(?:PROJCS|GEOGCS|PROJCRS|GEOGCRS)\["([^"]+)"', prj or "")
    return m[1] if m else None


def create(db: Session, name: str | None, definition: str, show_in_cards: bool = True) -> CrsDef:
    definition = (definition or "").strip()
    check(db, definition)
    name = (name or "").strip() or name_from_prj(definition) or definition[:60]
    if db.scalar(sa.select(CrsDef.id).where(CrsDef.name == name)):
        raise EditError(f"Система координат «{name}» уже есть")
    c = CrsDef(name=name, definition=definition, show_in_cards=show_in_cards)
    db.add(c)
    db.flush()
    return c


def coords_in_all(db: Session, lon: float, lat: float) -> list[dict]:
    """Координаты точки во всех системах, отмеченных «показывать в карточках»."""
    out = []
    for c in db.scalars(sa.select(CrsDef).where(CrsDef.show_in_cards).order_by(CrsDef.name)):
        try:
            (x, y), = transform(db, [(lon, lat)], WGS84, c.definition)
        except EditError:
            continue
        geo = is_geographic(c.definition)
        out.append({"crs": c.name, "north": round(y, 7 if geo else 2), "east": round(x, 7 if geo else 2),
                    "geographic": geo})
    return out


COORD_PAIR = re.compile(r"^\s*(-?\d{3,8}(?:[.,]\d+)?)\s*[;, ]\s*(-?\d{3,8}(?:[.,]\d+)?)\s*$")


def find_point(db: Session, text: str) -> list[dict]:
    """Поиск по координатам в местной системе: «X Y» (север, восток — как в российских документах).

    Пробуются все системы из списка; подходит та, где точка попадает в район сети (до 50 км).
    Возвращает варианты [{crs, lon, lat, distance_km}] — ближайший к сети первым.
    """
    m = COORD_PAIR.match(text or "")
    if not m:
        return []
    a, b = float(m[1].replace(",", ".")), float(m[2].replace(",", "."))
    center = db.execute(sa.text("SELECT ST_X(c), ST_Y(c) FROM (SELECT ST_Centroid(ST_Extent(geom)) c "
                                "FROM network_node) q")).first()
    out = []
    for c in db.scalars(sa.select(CrsDef)):
        if is_geographic(c.definition):
            continue
        for north, east in ((a, b), (b, a)):      # X-север Y-восток или наоборот
            try:
                (lon, lat), = transform(db, [(east, north)], c.definition, WGS84)
            except EditError:
                continue
            if not (-180 <= lon <= 180 and -90 <= lat <= 90):
                continue
            dist = None
            if center and center[0] is not None:
                dist = db.execute(sa.text("""SELECT ST_Distance(ST_SetSRID(ST_MakePoint(:a, :b), 4326)::geography,
                    ST_SetSRID(ST_MakePoint(:c, :d), 4326)::geography) / 1000"""),
                    {"a": lon, "b": lat, "c": center[0], "d": center[1]}).scalar()
                if dist > 50:
                    continue
            out.append({"crs": c.name, "lon": round(lon, 7), "lat": round(lat, 7),
                        "distance_km": round(dist, 1) if dist is not None else None,
                        "order": "X — север, Y — восток" if (north, east) == (a, b) else "X — восток, Y — север"})
    out.sort(key=lambda r: r["distance_km"] if r["distance_km"] is not None else 0)
    return out
