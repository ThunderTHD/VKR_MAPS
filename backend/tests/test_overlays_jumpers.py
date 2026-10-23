"""Этап 13: подложки (привязка скана по опорным точкам) и перемычки между объектами."""
import io
import math

import pytest
import sqlalchemy as sa
from PIL import Image

from app.models import Document, Jumper, MapOverlay
from app.services import jumpers, overlays, splicing
from app.services.editing import EditError


def _png(w=400, h=300) -> bytes:
    b = io.BytesIO()
    Image.new("RGB", (w, h), (240, 240, 230)).save(b, "PNG")
    return b.getvalue()


def _points(rot_deg=12.0, m_per_px=0.5, lon0=52.33, lat0=55.67, n=4):
    """Опорные точки по известному преобразованию: поворот, масштаб, сдвиг (в метрах Меркатора)."""
    x0, y0 = overlays.merc(lon0, lat0)
    k = math.cos(math.radians(lat0))
    s = m_per_px / k                    # метры Меркатора на пиксель
    a = math.radians(rot_deg)
    pts = []
    for px, py in [(0, 0), (400, 0), (0, 300), (400, 300)][:n]:
        x = x0 + s * (px * math.cos(a) + py * math.sin(a))
        y = y0 + s * (px * math.sin(a) - py * math.cos(a))
        lon, lat = overlays.unmerc(x, y)
        pts.append({"px": px, "py": py, "lon": lon, "lat": lat})
    return pts


def test_fit_exact_affine():
    pts = _points()
    res = overlays.fit(pts, [400, 300])
    assert res["rms_m"] < 0.01 and max(res["residuals_m"]) < 0.01
    assert res["m_per_px"] == pytest.approx(0.5, rel=1e-3)
    assert res["corners"]["tl"] == pytest.approx([pts[0]["lon"], pts[0]["lat"]], abs=1e-9)
    assert res["corners"]["br"] == pytest.approx([pts[3]["lon"], pts[3]["lat"]], abs=1e-9)
    # Ошибка в одной точке видна в невязках
    bad = [dict(p) for p in pts]
    bad[3]["lat"] += 0.0002     # ≈22 м
    assert overlays.fit(bad, [400, 300])["rms_m"] > 3
    with pytest.raises(EditError, match="три"):
        overlays.fit(pts[:2], [400, 300])
    line = [{"px": i * 10, "py": i * 10, "lon": 52 + i * 1e-4, "lat": 55 + i * 1e-4} for i in range(3)]
    with pytest.raises(EditError, match="одной прямой"):
        overlays.fit(line, [400, 300])


def test_overlay_lifecycle(db):
    ov = overlays.create(db, filename="план.png", data=_png(), name=None)
    assert ov.name == "план" and ov.control_points["size"] == [400, 300] and not ov.visible
    with pytest.raises(EditError, match="привяжите"):
        overlays.update(db, ov, {"visible": True})
    overlays.set_points(db, ov, _points())
    s = overlays.summary(ov)
    assert s["visible"] and s["corners"] and len(s["points"]) == 4 and s["url"].startswith("/api/documents/")
    overlays.update(db, ov, {"opacity": 5, "name": "Ситуационный план"})
    assert ov.opacity == 1.0 and ov.name == "Ситуационный план"
    doc_id = ov.document_id
    overlays.delete(db, ov, None)
    assert db.get(Document, doc_id) is None and db.scalar(sa.select(sa.func.count(MapOverlay.id))) == 0
    with pytest.raises(EditError, match="картинка"):
        overlays.create(db, filename="x.txt", data=b"hello", name=None)


def test_overlay_from_pdf(db):
    reportlab = pytest.importorskip("reportlab")
    from reportlab.pdfgen import canvas
    buf = io.BytesIO()
    c = canvas.Canvas(buf, pagesize=(595, 842))
    c.line(50, 50, 500, 700)
    c.showPage()
    c.save()
    assert reportlab
    ov = overlays.create(db, filename="план.pdf", data=buf.getvalue(), name="план")
    w, h = ov.control_points["size"]
    assert max(w, h) <= overlays.MAX_SIDE and h > w
    with pytest.raises(EditError, match="страницы 3 нет"):
        overlays.create(db, filename="план.pdf", data=buf.getvalue(), name="план", page=3)


@pytest.fixture()
def net(db):
    from tests.test_localization import _build
    n = _build(db)
    n["closure"] = splicing.create_closure(db, node_id=n["n"]["B"].id, number="М5")
    return n


def test_nearest_prefers_closure(db, net):
    b = net["n"]["B"]
    lon, lat = 50.0016, 55.0
    hit = jumpers.nearest(db, lon + 0.00005, lat, 25)
    assert hit["type"] == "closure" and hit["id"] == net["closure"].id and hit["distance_m"] < 5
    assert jumpers.nearest(db, lon + 0.01, lat, 25) is None
    assert b.id  # точка B существует и тоже в радиусе, но муфта важнее


def test_jumper_create_card_delete(db, net):
    d = net["n"]["D"]
    j = jumpers.create(db, a_type="closure", a_id=net["closure"].id, b_type="node", b_id=d.id,
                       label="Резерв 24 волокна")
    card = jumpers.card(db, j.id)
    assert card["title"] == "Резерв 24 волокна" and len(card["sections"][0]["items"]) == 2
    assert card["sections"][0]["items"][0]["type"] == "closure"
    length = float(card["fields"][1][1].split()[0])
    assert 100 < length < 120          # B—D ≈ 111 м
    jumpers.update(db, j, "  ")
    assert j.label is None
    with pytest.raises(EditError, match="совпадают"):
        jumpers.create(db, a_type="node", a_id=d.id, b_type="node", b_id=d.id, label=None)
    with pytest.raises(EditError, match="не найден"):
        jumpers.create(db, a_type="cross", a_id=999999, b_type="node", b_id=d.id, label=None)
    jumpers.delete(db, j)
    assert db.get(Jumper, j.id) is None


def test_api_roles(client):
    from app.core.db import SessionLocal
    from app.core.security import hash_password
    from app.models import AppUser, Role
    pw = "Тест-пароль-13"
    with SessionLocal() as s:
        s.execute(sa.delete(AppUser).where(AppUser.login == "o_view"))
        s.add(AppUser(login="o_view", full_name="o", role=Role.viewer, password_hash=hash_password(pw)))
        s.commit()
    assert client.post("/api/auth/login", json={"login": "o_view", "password": pw}).status_code == 200
    H = {"X-Requested-With": "fetch"}
    assert client.get("/api/overlays").status_code == 200
    assert client.post("/api/overlays", headers=H, files={"file": ("a.png", _png(), "image/png")}).status_code == 403
    assert client.post("/api/jumpers", headers=H, json={"a_type": "node", "a_id": 1, "b_type": "node",
                                                        "b_id": 2}).status_code == 403
    assert client.get("/api/crs").status_code == 200


def test_pwa_files(client):
    """Приложение устанавливается на телефон: манифест, service worker и иконки отдаются с корня."""
    m = client.get("/manifest.webmanifest")
    assert m.status_code == 200 and m.json()["display"] == "standalone"
    for icon in m.json()["icons"]:
        assert client.get(icon["src"]).status_code == 200
    sw = client.get("/sw.js")
    assert sw.status_code == 200 and "javascript" in sw.headers["content-type"]
    assert 'rel="manifest"' in client.get("/").text


def test_coordinate_search_short_decimals(db):
    from app.services.cards import _coordinate_hits
    hit, = _coordinate_hits(db, "55,68, 52,35")
    assert (hit["lat"], hit["lon"]) == (55.68, 52.35)
