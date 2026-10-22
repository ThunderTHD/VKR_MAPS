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
