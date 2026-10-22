"""Этап 9: разбор пакета ИД, проверки, сопоставление с сетью, подтверждение."""
import time

import pytest
import sqlalchemy as sa

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import (
    AccessNode,
    AppUser,
    AsBuiltPackage,
    Cable,
    Document,
    Instrument,
    Measurement,
    NetworkNode,
    PackageStatus,
    Role,
    RouteSegment,
)
from app.services import splicing
from app.services.asbuilt import apply, parser
from app.services.editing import EditError
from app.services.generator import reset_network
from tests.asbuilt_factory import POLES, font_path, sample_pdf
from tests.test_localization import _build

pytestmark = pytest.mark.skipif(font_path() is None, reason="нет шрифта DejaVuSans для синтетического PDF")


@pytest.fixture(scope="module")
def sample_file(tmp_path_factory):
    path = tmp_path_factory.mktemp("asbuilt") / "ИД.pdf"
    path.write_bytes(sample_pdf())
    return path


@pytest.fixture(scope="module")
def parsed(sample_file):
    return parser.parse_pdf(sample_file, ocr=True)


def test_rules_from_yaml():
    assert parser.classify("Схема разварки ОВ в муфтах") == "splice_scheme"
    assert parser.classify("Протокол монтажа оптической муфты") == "closure_protocol"
    assert parser.classify("что-то непонятное") == "unknown"
    assert "skeleton" in parser.PAGE_TITLES and "certificate" in parser.ARCHIVE_ONLY


def test_parse_summary_and_pages(parsed):
    s = parsed["summary"]
    assert s["volp"] == "99000001/01" and s["client_code"] == "10000001"
    assert s["contractor"] == "ООО «Тест-Связь»" and s["doc_date"] == "2026-08-16"
    kinds = [p["kind"] for p in parsed["pages"]]
    for k in ("act_pnr", "line_passport", "skeleton", "route_scheme", "closure_protocol", "cross_protocol", "otdr",
              "power_meter", "cable_report", "insulation", "poles"):
        assert k in kinds, k
    ex = parsed["extracted"]
    assert ex["skeleton"]["physical_m"] == 320 and ex["skeleton"]["optical_m"] == 321.5
    assert ex["otdr"]["rows"][0]["optical_km"] == 2.54
    assert len(ex["poles"]["poles"]) == len(POLES)
    assert parsed["trace"][0].startswith("ODF-4 порт 12")


def test_planted_errors_found(parsed):
    codes = {f["code"] for f in parsed["findings"]}
    assert {"VOLP_MISMATCH", "LENGTH_MISMATCH", "PERSON_INITIALS", "FIBER_COLOR", "INSULATION_KM"} <= codes
    ports = next(f for f in parsed["findings"] if f["code"] == "PORTS")
    assert "совпадают" in ports["message"]
    if parser.ocr_available():  # в Docker-образе и CI есть Tesseract с русским
        assert "CALIBRATION_EXPIRED" in codes
    else:
        assert "OCR_OFF" in codes


# ------------------------------------------------------------------ сопоставление и подтверждение


@pytest.fixture()
def net(db, sample_file):
    n = _build(db)
    x = n["x"]
    x.name = "СЛ 99000001/01"
    db.flush()
    # Здание (C): кросс ODF-1, коммутатор из акта ПНР; узел (A): кросс ODF-4 на 16 портов
    db.execute(sa.text("UPDATE optical_cross SET name = 'ODF-1' WHERE node_id = :c"), {"c": n["n"]["C"].id})
    odf4 = db.execute(sa.text("UPDATE optical_cross SET name = 'ODF-4' WHERE node_id = :a RETURNING id"),
                      {"a": n["n"]["A"].id}).scalar_one()
    db.expire_all()  # названия кроссов изменены SQL-запросом
    db.add(AccessNode(hostname="99TSTX-ACCF00-001", node_id=n["n"]["C"].id))
    db.add(AccessNode(hostname="99TSTX-AGGF00-DSW1", node_id=n["n"]["A"].id))
    db.flush()
    cross4 = db.get(splicing.OpticalCross, odf4)
    splicing.update_cross(db, cross4, {"port_count": 16}, None)
    pkg = apply.create_package(db, "ИД.pdf", sample_file.read_bytes(), None)
    pkg.parse_result = parser.parse_pdf(sample_file, ocr=False)
    pkg.status = PackageStatus.parsed
    pkg.volp = "99000001/01"
    db.flush()
    return {**n, "pkg": pkg}


def test_suggest(db, net):
    s = apply.suggest(db, net["pkg"])
    assert s["cable_id"] == net["x"].id
    assert s["port_a"]["id"] == net["port_c"] and s["port_a"]["cross"] == "ODF-1"  # здание, порт 1
    assert s["port_b"]["cross"] == "ODF-4" and s["port_b"]["port_no"] == 12
    assert s["access_node"]["exists_id"] and s["access_node"]["uplink_exists_id"]
    assert s["measurement"]["optical_m"] == 2540 and s["twist"] == pytest.approx(0.0046875, abs=1e-5)
    assert {i["kind"] for i in s["instruments"]} == {"splicer", "otdr", "power_meter"}


def test_compare_with_system(db, net):
    s = apply.suggest(db, net["pkg"])
    found = {f["code"]: f for f in apply.compare(db, net["pkg"], net["x"].id, s["port_a"]["id"], s["port_b"]["id"])}
    assert found["SYS_LENGTH"]["level"] == "info"            # 100 + 200 + 20 запаса = 320 м, как в ИД
    assert "SYS_RESERVES" not in found and "SYS_FIBERS" not in found
    assert found["SYS_TRACE"]["level"] == "error"            # в системе волокно 1 приходит в порт 1, а не 12
    ok = {f["code"]: f for f in apply.compare(db, net["pkg"], net["x"].id, net["port_c"], net["port_a"])}
    assert ok["SYS_OPTICAL"]["level"] == "warning" and "эталоном" in ok["SYS_OPTICAL"]["message"]


def test_confirm_writes_everything(db, net):
    pkg = net["pkg"]
    nodes_before = db.scalar(sa.select(sa.func.count(NetworkNode.id)))
    done = apply.confirm(db, pkg, cable_id=net["x"].id, port_a_id=net["port_c"], port_b_id=net["port_a"], poles=True)
    m = db.get(Measurement, done["measurement_id"])
    assert m.optical_length_m == 2540 and m.package_id == pkg.id and m.instrument is not None
    cable = db.get(Cable, net["x"].id)
    assert cable.twist_source == "as_built" and cable.twist_factor == pytest.approx(0.00469, abs=1e-5)
    assert cable.twist_err_2s == pytest.approx(0.82 / 320, rel=0.01)
    an = db.get(AccessNode, done["access_node_id"])
    assert an.ip is not None and str(an.ip) == "10.10.10.10" and an.cross_port_id == net["port_c"]
    assert an.model == "TEST-SW-24" and an.uplink is not None
    assert db.scalar(sa.select(sa.func.count(Instrument.id)).where(Instrument.serial == "777001")) == 1
    assert done["poles"]["poles_created"] == len(POLES) and done["poles"]["spans_created"] == len(POLES) - 1
    assert db.scalar(sa.select(sa.func.count(NetworkNode.id))) == nodes_before + len(POLES)
    assert db.scalar(sa.select(sa.func.count(RouteSegment.id)).where(RouteSegment.kind == "aerial")) == len(POLES) - 1
    doc = db.get(Document, pkg.document_id)
    assert (doc.owner_type, doc.owner_id) == ("cable", net["x"].id)
    assert pkg.status == PackageStatus.confirmed and pkg.cable_id == net["x"].id
    assert pkg.check_report["applied"]["measurement_id"] == m.id
    with pytest.raises(EditError, match="только разобранный"):
        apply.confirm(db, pkg, cable_id=None, port_a_id=None, port_b_id=None)


def test_confirm_requires_ports_for_measurement(db, net):
    with pytest.raises(EditError, match="порты тракта"):
        apply.confirm(db, net["pkg"], cable_id=net["x"].id, port_a_id=None, port_b_id=None)
    # Без измерения — можно
    done = apply.confirm(db, net["pkg"], cable_id=net["x"].id, port_a_id=None, port_b_id=None, measurement=False,
                         access_node=False)
    assert "measurement_id" not in done and done["twist"]


def test_poles_reuse_existing_points(db, net):
    lat, lon = POLES[0]
    db.add(NetworkNode(kind="pole", code="оп.1", geom=f"SRID=4326;POINT({lon} {lat + 0.00001})"))  # ≈1 м
    db.flush()
    res = apply._add_poles(db, net["pkg"].parse_result["extracted"]["poles"]["poles"], None)
    assert res["poles_reused"] == 1 and res["poles_created"] == len(POLES) - 1


def test_upload_rejects_non_pdf(db):
    with pytest.raises(EditError, match="не PDF"):
        apply.create_package(db, "x.pdf", b"<html>", None)


# ------------------------------------------------------------------ API

PASSWORD = "Тест-пароль-9"
H = {"X-Requested-With": "fetch"}


@pytest.fixture()
def api_users(client):
    with SessionLocal() as s:
        s.execute(sa.delete(AppUser).where(AppUser.login.in_(["a_admin", "a_inst", "a_view"])))
        for login, role in (("a_admin", Role.admin), ("a_inst", Role.installer), ("a_view", Role.viewer)):
            s.add(AppUser(login=login, full_name=login, role=role, password_hash=hash_password(PASSWORD)))
        s.commit()
    yield client
    with SessionLocal() as s:
        reset_network(s)
        s.execute(sa.text("TRUNCATE asbuilt_package, document RESTART IDENTITY CASCADE"))
        s.commit()


def _login(c, who):
    assert c.post("/api/auth/login", json={"login": who, "password": PASSWORD}).status_code == 200


def test_api_upload_process_and_reject(api_users, sample_file):
    c = api_users
    _login(c, "a_view")
    assert c.get("/api/asbuilt").status_code == 403
    _login(c, "a_inst")
    files = {"file": ("ИД.pdf", sample_file.read_bytes(), "application/pdf")}
    assert c.post("/api/asbuilt", headers=H, files=files, data={"ocr": "false"}).status_code == 403
    _login(c, "a_admin")
    r = c.post("/api/asbuilt", headers=H, files=files, data={"ocr": "false"})
    assert r.status_code == 200 and r.json()["status"] == "processing"
    pid = r.json()["id"]
    for _ in range(60):  # разбор идёт в фоновом потоке
        body = c.get(f"/api/asbuilt/{pid}").json()
        if body["status"] != "processing":
            break
        time.sleep(0.5)
    assert body["status"] == "parsed", body.get("error")
    assert body["summary"]["volp"] == "99000001/01" and body["suggest"]["measurement"]["optical_m"] == 2540
    assert any(f["code"] == "LENGTH_MISMATCH" for f in body["findings"])
    assert c.post(f"/api/asbuilt/{pid}/compare", headers=H, json={}).status_code == 200
    assert c.post(f"/api/asbuilt/{pid}/reject", headers=H, json={"reason": "вернуть подрядчику"}).status_code == 200
    with SessionLocal() as s:
        assert s.get(AsBuiltPackage, pid).status == PackageStatus.rejected
    r = c.post("/api/asbuilt", headers=H, files={"file": ("a.pdf", b"not a pdf", "application/pdf")})
    assert r.status_code == 409
