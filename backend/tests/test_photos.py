"""Этап 11: координаты с фото (EXIF, подпись на кадре) и точки сети по фото."""
import io
import shutil

import pytest
import sqlalchemy as sa
from PIL import Image, ImageDraw, ImageFont

from app.models import Document, NetworkNode
from app.services import documents, photo_coords, photo_points
from app.services.editing import EditError

FONT = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
LAT, LON = 55.7012, 52.3457


def exif_jpeg(lat=LAT, lon=LON, when="2026:10:20 14:05:00") -> bytes:
    img = Image.new("RGB", (640, 480), (90, 120, 90))
    ex = Image.Exif()
    gps = ex.get_ifd(0x8825)

    def dms(v):
        d = int(v)
        m = int((v - d) * 60)
        return (float(d), float(m), round((v - d - m / 60) * 3600, 3))
    gps.update({1: "N" if lat >= 0 else "S", 2: dms(abs(lat)), 3: "E" if lon >= 0 else "W", 4: dms(abs(lon))})
    ex[0x8825] = gps
    ex.get_ifd(0x8769)[36867] = when
    b = io.BytesIO()
    img.save(b, "JPEG", exif=ex)
    return b.getvalue()


def stamped_jpeg(text: str) -> bytes:
    """Кадр с впечатанной подписью, как у приложений «GPS-камера»."""
    img = Image.new("RGB", (1200, 800), (60, 80, 60))
    d = ImageDraw.Draw(img)
    d.rectangle((0, 680, 1200, 800), fill=(0, 0, 0))
    f = ImageFont.truetype(FONT, 30)
    d.text((20, 700), "20.10.2026 14:05  ул. Тестовая 1", font=f, fill="white")
    d.text((20, 745), text, font=f, fill="white")
    b = io.BytesIO()
    img.save(b, "JPEG")
    return b.getvalue()


def test_exif():
    m = photo_coords.extract(exif_jpeg(), "a.jpg")
    assert m["source"] == "exif"
    assert m["lat"] == pytest.approx(LAT, abs=1e-5) and m["lon"] == pytest.approx(LON, abs=1e-5)
    assert m["taken_at"].hour == 14
    m = photo_coords.extract(exif_jpeg(-33.86, -70.65), "b.jpg")
    assert m["lat"] < 0 and m["lon"] < 0


@pytest.mark.parametrize("text,expected", [
    ("Lat 55.701234 N Long 52.345678 E", (55.701234, 52.345678)),
    ("55,701234° N, 52,345678° E", (55.701234, 52.345678)),
    ("Широта: 55.701234 Долгота: 52.345678", (55.701234, 52.345678)),
    ("55°42'04.4\" N 52°20'44.4\" E", (55.701222, 52.345667)),
    ("55°42'04.4\" М 52°20'44.4\" Е", (55.701222, 52.345667)),   # кириллица после OCR
    ("Lat -33.868800 Long -70.650000", (-33.8688, -70.65)),
    ("20.10.2026 14:05 ул. Садовая 13", None),
])
def test_parse_text(text, expected):
    got = photo_coords.parse_text(text)
    if expected is None:
        assert got is None
    else:
        assert got == pytest.approx(expected, abs=1e-5)


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="нет Tesseract")
def test_ocr_stamp():
    m = photo_coords.extract(stamped_jpeg("Lat 55.701234° Long 52.345678°"), "c.jpg")
    assert m["source"] == "ocr" and m["lat"] == pytest.approx(55.701234) and m["lon"] == pytest.approx(52.345678)


def test_not_image():
    assert photo_coords.extract(b"%PDF-1.4", "x.pdf") is None
    assert photo_coords.extract(b"garbage", "x.jpg") is None


def _node(db, lat, lon, kind="well", code="к.1"):
    n = NetworkNode(kind=kind, code=code, geom=f"SRID=4326;POINT({lon} {lat})")
    db.add(n)
    db.flush()
    return n


def test_attach_stores_coords_and_distance(db):
    n = _node(db, LAT, LON)
    documents.attach(db, owner_type="node", owner_id=n.id, filename="IMG_1.jpg",
                     data=exif_jpeg(LAT + 0.0001, LON), kind="photo")
    d = documents.list_documents(db, "node", n.id)[0]
    assert d["coord_source"] == "exif" and d["lonlat"] and 9 < d["distance_m"] < 13 and d["taken_at"]


def test_photo_points_flow(db):
    near = _node(db, LAT, LON, code="к.900")
    files = [("near.jpg", exif_jpeg(LAT + 0.00005, LON)),            # ≈5 м от колодца
             ("far.jpg", exif_jpeg(LAT + 0.002, LON)),               # ≈220 м — новая точка
             ("far2.jpg", exif_jpeg(LAT + 0.002, LON + 0.00001)),    # тот же колодец с другой стороны
             ("nocoords.png", _plain_png())]
    items = photo_points.analyze(db, files, None)
    by = {i["filename"]: i for i in items}
    assert by["near.jpg"]["suggest"] == "attach" and by["near.jpg"]["nearest"]["id"] == near.id
    assert by["far.jpg"]["suggest"] == "create" and by["nocoords.png"]["coords"] is None
    before = db.scalar(sa.select(sa.func.count(NetworkNode.id)))
    res = photo_points.commit(db, [
        {"doc_id": by["near.jpg"]["doc_id"], "action": "attach", "node_id": near.id},
        {"doc_id": by["far.jpg"]["doc_id"], "action": "create", "kind": "pole", "code": "оп.77"},
        {"doc_id": by["far2.jpg"]["doc_id"], "action": "create", "kind": "pole"},
        {"doc_id": by["nocoords.png"]["doc_id"], "action": "skip"},
    ], None)
    assert res == {"attached": 3, "created": 1, "skipped": 1, "nodes": res["nodes"]}
    assert db.scalar(sa.select(sa.func.count(NetworkNode.id))) == before + 1
    pole = db.get(NetworkNode, res["nodes"][0])
    assert pole.kind.value == "pole" and pole.code == "оп.77"
    assert len(documents.list_documents(db, "node", pole.id)) == 2
    assert db.get(Document, by["nocoords.png"]["doc_id"]) is None
    with pytest.raises(EditError, match="не найдено"):   # повторно подтвердить нельзя
        photo_points.commit(db, [{"doc_id": by["near.jpg"]["doc_id"], "action": "skip"}], None)


def _plain_png() -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (64, 64), (200, 200, 200)).save(b, "PNG")
    return b.getvalue()


def test_photo_points_validation(db):
    with pytest.raises(EditError, match="не фото"):
        photo_points.analyze(db, [("a.pdf", b"%PDF")], None)
    with pytest.raises(EditError, match="Не выбрано"):
        photo_points.analyze(db, [], None)
    item = photo_points.analyze(db, [("x.png", _plain_png())], None)[0]
    with pytest.raises(EditError, match="нет координат"):
        photo_points.commit(db, [{"doc_id": item["doc_id"], "action": "create"}], None)


# ------------------------------------------------------------------ муфта по фото


def notecam_jpeg(lat: float, lon: float, accuracy: float = 10.97) -> bytes:
    """Кадр с подписью как у NoteCam: светлый текст в левом нижнем углу."""
    img = Image.new("RGB", (1180, 666), (170, 175, 180))
    d = ImageDraw.Draw(img)
    f = ImageFont.truetype(FONT, 22)
    d.rectangle((0, 540, 250, 666), fill=(245, 245, 245))
    for i, t in enumerate([f"Широта: {lat:.6f}", f"Долгота: {lon:.6f}", "Возвышение: 95.19±7.8 м",
                           f"Точность: {accuracy} м", "Время: 31-08-2026 16:31"]):
        d.text((4, 543 + i * 24), t, font=f, fill=(20, 20, 20))
    b = io.BytesIO()
    img.save(b, "JPEG", quality=92)
    return b.getvalue()


@pytest.fixture()
def lines(db):
    from tests.test_localization import _build
    return _build(db)   # A —100 м— B —200 м— C, B — D; кабель X: A→C, Y: B→D


def test_closure_at_nearby_well(db, lines):
    from app.services import closure_photo
    b = lines["n"]["B"]   # долгота 50.0016, широта 55.0
    r = closure_photo.place(db, filename="m1.jpg", data=exif_jpeg(55.0 + 0.00004, 50.0016))
    assert r["mode"] == "node" and r["node_id"] == b.id and r["distance_m"] < 6 and not r["needs_data"]
    assert "в колодце" in r["message"]
    docs = documents.list_documents(db, "closure", r["closure_id"])
    assert len(docs) == 1 and docs[0]["coord_source"] == "exif" and docs[0]["distance_m"] < 6


def test_closure_on_segment(db, lines):
    from app.models import Closure
    from app.services import closure_photo, splicing
    # Середина участка A—B (≈50 м до колодцев), 8 м в сторону от линии
    r = closure_photo.place(db, filename="m2.jpg", data=exif_jpeg(55.0 + 0.00007, 50.0008), number="М7")
    assert r["mode"] == "segment" and 6 < r["distance_m"] < 10
    node = db.get(NetworkNode, r["node_id"])
    assert node.kind.value == "closure_point"
    # Кабель X, проходивший по участку, теперь проходит через новую точку — в муфте его можно разварить
    assert lines["x"].id in {c.id for c in splicing.transit_cables(db, node.id)}
    assert db.get(Closure, r["closure_id"]).number == "М7"


def test_closure_without_lines(db, lines):
    from app.models import Closure
    from app.services import closure_photo
    r = closure_photo.place(db, filename="m3.jpg", data=exif_jpeg(55.01, 50.01))
    assert r["mode"] == "free" and r["needs_data"]
    clo = db.get(Closure, r["closure_id"])
    assert "нет линий" in clo.note and "нет линий" in db.get(NetworkNode, r["node_id"]).note


def test_closure_photo_without_coords(db, lines):
    from app.services import closure_photo
    with pytest.raises(EditError, match="не найдены координаты"):
        closure_photo.place(db, filename="x.png", data=_plain_png())


@pytest.mark.skipif(shutil.which("tesseract") is None, reason="нет Tesseract")
def test_notecam_stamp_with_accuracy():
    m = photo_coords.extract(notecam_jpeg(55.674239, 52.330327), "notecam.jpg")
    assert m["source"] == "ocr" and m["lat"] == pytest.approx(55.674239) and m["lon"] == pytest.approx(52.330327)
    assert m["accuracy_m"] == pytest.approx(10.97) and m["taken_at"].month == 8


def test_parse_extra():
    e = photo_coords.parse_extra("Точность: 10.97 м\nВремя: 31-08-2026 16:31")
    assert e["accuracy_m"] == pytest.approx(10.97) and e["taken_at"].day == 31
    assert photo_coords.parse_extra("TouHoctb: 5,5 M")["accuracy_m"] == pytest.approx(5.5)
    assert photo_coords.parse_text("Wwupota: 55.674239\nDonrota: 52.330327") == pytest.approx((55.674239, 52.330327))
