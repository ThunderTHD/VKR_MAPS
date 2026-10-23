"""Файлы для демонстрации: фото, «скан» плана и Shapefile, согласованные с текущей сетью.

Демонстрационные сценарии (docs/guide/demo-scenarios.md) показывают загрузку фото колодцев,
«Муфту по фото», привязку плана и импорт Shapefile из местной системы координат. Для этого
нужны входные файлы, которые совпадают с сетью в базе: фото должно быть снято рядом с
существующим колодцем, на плане — те же колодцы, что на карте. Поэтому файлы не рисуются
вручную, а строятся по текущей сети (после generate.bat). Все данные — сгенерированные.

Что создаётся (команда `python -m app.cli demo-files --out <папка>`):
  foto_kolodec_1.jpg, foto_kolodec_2.jpg — координаты в EXIF в 3–5 м от колодцев;
  foto_novaya_tochka.jpg — координаты в стороне от сети (будет предложена новая точка);
  mufta_po_foto.jpg — подпись координат на кадре, как у NoteCam, рядом с трассой без колодца;
  plan_kvartala.png — условная схема канализации квартала (повёрнута, как скан);
  novaya_ulica_utm39.zip — Shapefile «новой улицы» в UTM 39N (метры) с файлом .prj;
  utm39_primer.prj — та же система координат отдельным файлом (пример вместо МСК);
  README.txt — какие объекты на что рассчитаны.
"""
import io
import math
import random
from pathlib import Path

import sqlalchemy as sa
from PIL import Image, ImageDraw, ImageFilter, ImageFont
from sqlalchemy.orm import Session

from app.services import crs, shapefile_io
from app.services.shapefile_io import Feature

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
UTM = "EPSG:32639"


def _font(size: int, bold: bool = False):
    try:
        return ImageFont.truetype(FONT_BOLD if bold else FONT, size)
    except OSError:          # шрифта нет (образ без fonts-dejavu-core) — встроенный, кириллица не видна
        return ImageFont.load_default(size)


def _offset(lon: float, lat: float, east_m: float, north_m: float) -> tuple[float, float]:
    return (lon + east_m / (111_320 * math.cos(math.radians(lat))), lat + north_m / 111_320)


def _exif_jpeg(lat: float, lon: float, caption: str, when: str = "2026:10:20 14:05:00") -> bytes:
    img = Image.new("RGB", (1024, 768), (96, 110, 96))
    d = ImageDraw.Draw(img)
    d.ellipse((362, 234, 662, 534), outline=(40, 40, 40), width=18, fill=(70, 72, 70))
    d.text((40, 40), caption, font=_font(34, True), fill=(250, 250, 250))
    d.text((40, 90), "демонстрационное фото, координаты в EXIF", font=_font(24), fill=(230, 230, 230))
    ex = Image.Exif()
    gps = ex.get_ifd(0x8825)

    def dms(v: float):
        deg = int(v)
        m = int((v - deg) * 60)
        return (float(deg), float(m), round((v - deg - m / 60) * 3600, 3))
    gps.update({1: "N", 2: dms(lat), 3: "E", 4: dms(lon)})
    ex[0x8825] = gps
    ex.get_ifd(0x8769)[36867] = when
    b = io.BytesIO()
    img.save(b, "JPEG", quality=88, exif=ex)
    return b.getvalue()


def _notecam_jpeg(lat: float, lon: float, accuracy: float) -> bytes:
    """Кадр с подписью как у приложения NoteCam: светлая плашка в левом нижнем углу."""
    img = Image.new("RGB", (1180, 666), (150, 156, 150))
    d = ImageDraw.Draw(img)
    d.rounded_rectangle((420, 150, 760, 430), radius=60, fill=(30, 30, 32))     # корпус муфты
    d.text((40, 40), "Муфта (демонстрационное фото)", font=_font(30, True), fill=(250, 250, 250))
    d.rectangle((0, 540, 330, 666), fill=(245, 245, 245))
    f = _font(22)
    for i, t in enumerate([f"Широта: {lat:.6f}", f"Долгота: {lon:.6f}", "Возвышение: 95.19±7.8 м",
                           f"Точность: {accuracy} м", "Время: 20-10-2026 14:05"]):
        d.text((4, 543 + i * 24), t, font=f, fill=(20, 20, 20))
    b = io.BytesIO()
    img.save(b, "JPEG", quality=92)
    return b.getvalue()


def _wells_near_center(db: Session, n: int) -> list[dict]:
    """Колодцы, через которые идут кабели, — ближе всего к центру сети."""
    return [dict(r) for r in db.execute(sa.text("""
        WITH c AS (SELECT ST_SetSRID(ST_Centroid(ST_Extent(geom)), 4326) g FROM network_node)
        SELECT n.id, n.code, ST_X(n.geom) lon, ST_Y(n.geom) lat
        FROM network_node n, c
        WHERE n.kind = 'well' AND n.code IS NOT NULL
          AND EXISTS (SELECT 1 FROM route_segment s JOIN cable_route cr ON cr.segment_id = s.id
                      WHERE n.id IN (s.node_a_id, s.node_b_id))
        ORDER BY n.geom <-> c.g LIMIT :n"""), {"n": n}).mappings()]


def _free_spot(db: Session, lon: float, lat: float) -> tuple[float, float]:
    """Место рядом с точкой, но в 60+ м от любой трассы и точки сети."""
    for r in range(80, 600, 40):
        for ang in range(0, 360, 30):
            x, y = _offset(lon, lat, r * math.cos(math.radians(ang)), r * math.sin(math.radians(ang)))
            near = db.scalar(sa.text("""
                SELECT EXISTS (SELECT 1 FROM route_segment WHERE ST_DWithin(geom::geography,
                    ST_SetSRID(ST_MakePoint(:x, :y), 4326)::geography, 60))
                    OR EXISTS (SELECT 1 FROM network_node WHERE ST_DWithin(geom::geography,
                    ST_SetSRID(ST_MakePoint(:x, :y), 4326)::geography, 60))"""), {"x": x, "y": y})
            if not near:
                return x, y
    return _offset(lon, lat, 700, 0)


def _segment_for_closure(db: Session, exclude: list[int]) -> dict | None:
    """Длинный участок канализации с кабелем: середина — в 40+ м от колодцев."""
    row = db.execute(sa.text("""
        WITH c AS (SELECT ST_SetSRID(ST_Centroid(ST_Extent(geom)), 4326) g FROM network_node)
        SELECT s.id, ST_X(ST_LineInterpolatePoint(s.geom, 0.5)) lon, ST_Y(ST_LineInterpolatePoint(s.geom, 0.5)) lat,
               a.code a_code, b.code b_code, ST_Length(s.geom::geography) len
        FROM route_segment s JOIN network_node a ON a.id = s.node_a_id JOIN network_node b ON b.id = s.node_b_id, c
        WHERE s.kind = 'duct' AND ST_Length(s.geom::geography) > 120
          AND EXISTS (SELECT 1 FROM cable_route cr WHERE cr.segment_id = s.id)
          AND NOT (s.node_a_id = ANY(:ex) OR s.node_b_id = ANY(:ex))
        ORDER BY s.geom <-> c.g LIMIT 1"""), {"ex": exclude}).mappings().first()
    return dict(row) if row else None


def _plan(db: Session, lon: float, lat: float, half_w: float = 260, half_h: float = 190) -> tuple[bytes, list[str]]:
    """Условная схема канализации квартала вокруг точки — как скан бумажного плана."""
    (cx, cy), = crs.transform(db, [(lon, lat)], crs.WGS84, UTM)
    env = (cx - half_w, cy - half_h, cx + half_w, cy + half_h)
    segs = db.execute(sa.text("""
        SELECT ST_AsGeoJSON(ST_Intersection(ST_Transform(s.geom, 32639), e)) g
        FROM route_segment s, ST_MakeEnvelope(:x1, :y1, :x2, :y2, 32639) e
        WHERE ST_Intersects(ST_Transform(s.geom, 32639), e)"""),
        {"x1": env[0], "y1": env[1], "x2": env[2], "y2": env[3]}).scalars().all()
    nodes = db.execute(sa.text("""
        SELECT coalesce(n.code, n.name) label, n.kind, ST_X(p) x, ST_Y(p) y
        FROM network_node n, ST_Transform(n.geom, 32639) p
        WHERE ST_Within(p, ST_MakeEnvelope(:x1, :y1, :x2, :y2, 32639)) AND n.kind <> 'closure_point'"""),
        {"x1": env[0], "y1": env[1], "x2": env[2], "y2": env[3]}).mappings().all()
    import json
    scale = 3.0                                  # точек на метр
    margin = 90
    w, h = int(2 * half_w * scale) + 2 * margin, int(2 * half_h * scale) + 2 * margin + 60

    def px(x: float, y: float) -> tuple[float, float]:
        return margin + (x - env[0]) * scale, margin + 60 + (env[3] - y) * scale
    img = Image.new("RGB", (w, h), (250, 248, 240))
    d = ImageDraw.Draw(img)
    d.rectangle((margin, margin + 60, w - margin, h - margin), outline=(60, 60, 60), width=3)
    d.text((margin, 30), "СХЕМА КАБЕЛЬНОЙ КАНАЛИЗАЦИИ КВАРТАЛА (условная, демонстрационная)",
           font=_font(30, True), fill=(30, 30, 30))
    for g in segs:
        geom = json.loads(g)
        lines = geom["coordinates"] if geom["type"] == "MultiLineString" else \
            [geom["coordinates"]] if geom["type"] == "LineString" else []
        for line in lines:
            pts = [px(x, y) for x, y in line]
            if len(pts) > 1:
                d.line(pts, fill=(40, 40, 40), width=5)
                d.line(pts, fill=(250, 248, 240), width=1)
    labels = []
    for n in nodes:
        x, y = px(n["x"], n["y"])
        r = 9 if n["kind"] == "well" else 12
        if n["kind"] in ("building", "site"):
            d.rectangle((x - 14, y - 10, x + 14, y + 10), outline=(30, 30, 30), width=3, fill=(230, 226, 214))
        else:
            d.ellipse((x - r, y - r, x + r, y + r), outline=(20, 20, 20), width=3, fill=(255, 255, 255))
        if n["label"] and n["kind"] == "well":
            d.text((x + 12, y - 30), str(n["label"]), font=_font(22), fill=(20, 20, 120))
            labels.append(str(n["label"]))
    # стрелка «север» и масштабная линейка
    d.polygon([(w - margin - 40, margin + 90), (w - margin - 52, margin + 130), (w - margin - 28, margin + 130)],
              fill=(30, 30, 30))
    d.text((w - margin - 48, margin + 135), "С", font=_font(24, True), fill=(30, 30, 30))
    d.line((margin + 20, h - margin - 30, margin + 20 + 100 * scale, h - margin - 30), fill=(30, 30, 30), width=4)
    d.text((margin + 20, h - margin - 62), "100 м", font=_font(22), fill=(30, 30, 30))
    # «скан»: небольшой поворот, сероватая бумага, лёгкое размытие и шум
    img = img.rotate(3.5, resample=Image.BICUBIC, expand=True, fillcolor=(236, 233, 224))
    img = img.filter(ImageFilter.GaussianBlur(0.6))
    rnd = random.Random(7)
    px_ = img.load()
    for _ in range(img.width * img.height // 60):
        x, y = rnd.randrange(img.width), rnd.randrange(img.height)
        v = rnd.randint(-25, 10)
        r_, g_, b_ = px_[x, y]
        px_[x, y] = (max(0, min(255, r_ + v)),) * 3 if r_ > 200 else (r_, g_, b_)
    out = io.BytesIO()
    img.save(out, "PNG", optimize=True)
    return out.getvalue(), labels


def _street_shapefile(db: Session) -> tuple[bytes, dict]:
    """«Новая улица»: 4 колодца и канализация от крайнего колодца сети наружу, в UTM 39N."""
    edge = db.execute(sa.text("""
        WITH c AS (SELECT ST_SetSRID(ST_Centroid(ST_Extent(geom)), 4326) g FROM network_node)
        SELECT n.id, n.code, ST_X(n.geom) lon, ST_Y(n.geom) lat, ST_X(c.g) clon, ST_Y(c.g) clat
        FROM network_node n, c WHERE n.kind = 'well' AND n.code IS NOT NULL
        ORDER BY ST_X(n.geom) DESC LIMIT 1""")).mappings().first()
    pts = [(edge["lon"], edge["lat"])]
    for i in range(1, 5):
        pts.append(_offset(edge["lon"], edge["lat"], 75 * i, 6 * i * (1 if i % 2 else -1)))
    utm = crs.transform(db, pts, crs.WGS84, UTM)
    prj = db.scalar(sa.text("SELECT srtext FROM spatial_ref_sys WHERE srid = 32639"))
    wells = [Feature("Point", [utm[i]], {"NAME": f"к.№100{i}", "ТИП": "колодец", "ПРИМ": "новая улица, проект"})
             for i in range(1, 5)]
    duct = [Feature("LineString", [utm[i - 1], utm[i]], {"ID": str(i), "ТИП": "канализация",
                                                         "КАНАЛОВ": "2"}) for i in range(1, 5)]
    files = {**shapefile_io.write_layer("Колодцы", "Point", wells, prj),
             **shapefile_io.write_layer("Канализация", "LineString", duct, prj)}
    return shapefile_io.zip_layers(files), {"from_code": edge["code"]}


def build(db: Session, out: Path) -> list[str]:
    """Создать демонстрационные файлы в папке out. Возвращает строки для README."""
    out.mkdir(parents=True, exist_ok=True)
    notes = []
    wells = _wells_near_center(db, 2)
    if len(wells) < 2:
        raise RuntimeError("В базе нет сети — сначала generate.bat")
    for i, w in enumerate(wells, 1):
        lon, lat = _offset(w["lon"], w["lat"], 3, 2.5)
        (out / f"foto_kolodec_{i}.jpg").write_bytes(_exif_jpeg(lat, lon, f"Колодец {w['code']}"))
        notes.append(f"foto_kolodec_{i}.jpg — снято в ~4 м от колодца {w['code']}: "
                     "«Точки по фото» предложит прикрепить к нему.")
    x, y = _free_spot(db, wells[0]["lon"], wells[0]["lat"])
    (out / "foto_novaya_tochka.jpg").write_bytes(_exif_jpeg(y, x, "Новая опора"))
    notes.append("foto_novaya_tochka.jpg — рядом нет точек сети: будет предложено поставить новую точку.")
    seg = _segment_for_closure(db, [w["id"] for w in wells])
    if seg:
        lon, lat = _offset(seg["lon"], seg["lat"], 2, -3)
        (out / "mufta_po_foto.jpg").write_bytes(_notecam_jpeg(lat, lon, 6.5))
        notes.append(f"mufta_po_foto.jpg — подпись как у NoteCam, середина участка {seg['a_code']} – "
                     f"{seg['b_code']} ({seg['len']:.0f} м): «Муфта по фото» поставит муфту на трассу.")
    png, labels = _plan(db, wells[0]["lon"], wells[0]["lat"])
    (out / "plan_kvartala.png").write_bytes(png)
    notes.append("plan_kvartala.png — условная схема канализации вокруг колодца "
                 f"{wells[0]['code']} (повёрнута на 3,5°, как скан). Для привязки щёлкайте колодцы "
                 f"по углам плана, например: {', '.join(labels[:6])}.")
    zdata, info = _street_shapefile(db)
    (out / "novaya_ulica_utm39.zip").write_bytes(zdata)
    prj = db.scalar(sa.text("SELECT srtext FROM spatial_ref_sys WHERE srid = 32639"))
    (out / "utm39_primer.prj").write_text(prj, encoding="utf-8")
    notes.append("utm39_primer.prj — файл системы координат (UTM 39N) для страницы «Системы координат»: "
                 "так добавляется МСК из .prj вашей ГИС.")
    notes.append(f"novaya_ulica_utm39.zip — Shapefile в UTM 39N (метры) с .prj: 4 новых колодца к.№1001–1004 и "
                 f"канализация от колодца {info['from_code']} на восток.")
    (out / "README.txt").write_text(
        "Демонстрационные файлы (сгенерированы по тестовой сети командой demo-files).\n"
        "Все данные вымышленные.\n\n" + "\n".join(notes) + "\n", encoding="utf-8")
    return notes
