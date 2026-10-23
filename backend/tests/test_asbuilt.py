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
from tests.asbuilt_factory import POLES, font_path, sample_docx, sample_pdf
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
    with pytest.raises(EditError, match="не PDF и не Word"):
        apply.create_package(db, "x.pdf", b"<html>", None)
    with pytest.raises(EditError, match="не PDF и не Word"):
        apply.create_package(db, "x.docx", b"PK\x03\x04 not a zip", None)


# ------------------------------------------------------------------ Word (.docx) и рассветка


def test_docx_same_as_pdf(parsed):
    """Тот же пакет в Word разбирается в те же данные и находит те же замечания."""
    d = parser.parse_docx(sample_docx(), ocr=parser.ocr_available())
    assert d["summary"]["format"] == "docx"
    assert [p["kind"] for p in d["pages"]] == [p["kind"] for p in parsed["pages"]]
    for k in ("summary", "extracted", "trace"):
        a, b = (dict(x) if isinstance(x, dict) else x for x in (parsed[k], d[k]))
        if k == "summary":
            b.pop("format")
        if k == "extracted":   # распознанный OCR номер свидетельства зависит от масштаба картинки
            a.pop("calibration", None)
            assert b.pop("calibration", {}).get("valid_until") == parsed[k].get("calibration", {}).get("valid_until")
        assert a == b, k
    assert sorted(f["code"] for f in d["findings"]) == sorted(f["code"] for f in parsed["findings"])


def test_splice_groups_from_protocols(parsed):
    groups = apply.splice_groups(parsed)
    clo = next(g for g in groups if g["kind"] == "closure_protocol")
    assert clo["cable_a"].startswith("ОКЛ-8") and clo["cable_b"].startswith("ОКГ-8")
    assert [(r["fiber_a"], r["fiber_b"]) for r in clo["rows"]] == [(2, 1), (3, 2)]
    assert clo["rows"][0]["loss_db"] == pytest.approx(0.02)
    assert apply._mark_key("ОКГ-8 (G.652.D)-T/П 3,0") == "ОКГ-8"
    assert apply._mark_key("ДПО-П-08У (2х4)-300Н") == "ДПО-П-08"


@pytest.fixture()
def splice_net(db, net):
    """Муфта М1 в B: из неё выходит кабель Y (ОКГ-8, «новый» из пакета), в неё приходит Z (ОКЛ-8)."""
    from app.models import CableType
    from app.services import routing
    okg = db.scalar(sa.select(CableType).where(CableType.mark.like("ОКГ-8%")))
    okl = db.scalar(sa.select(CableType).where(CableType.mark.like("ОКЛ-8%")))
    y = net["y"]
    y.cable_type_id = okg.id
    z = routing.create_cable(db, name="Z", operator_id=y.operator_id, cable_type_id=okl.id,
                             waypoints=[net["n"]["A"].id, net["n"]["B"].id])
    clo = splicing.create_closure(db, node_id=net["n"]["B"].id, number="М1")
    pkg = apply.create_package(db, "ИД.docx", sample_docx(scan=False), None)
    pkg.parse_result = parser.parse_docx(sample_docx(scan=False), ocr=False)
    pkg.status = PackageStatus.parsed
    db.flush()
    db.expire_all()
    return {**net, "z": z, "closure": clo, "pkg": pkg}


def test_splice_plan_and_confirm(db, splice_net):
    n = splice_net
    plan = apply.splice_plan(db, n["pkg"], n["y"].id)
    g = next(x for x in plan if x["kind"] == "closure_protocol")
    assert g["closure_id"] == n["closure"].id and g["cable_a_id"] == n["z"].id and g["cable_b_id"] == n["y"].id
    choice = [{"group": g["group"], "closure_id": g["closure_id"], "cable_a_id": g["cable_a_id"],
               "cable_b_id": g["cable_b_id"]}]
    done = apply.confirm(db, n["pkg"], cable_id=n["y"].id, port_a_id=None, port_b_id=None, measurement=False,
                         access_node=False, splices=choice)
    assert done["splices"] == {"created": 2, "already": 0}
    sheet = splicing.closure_sheet(db, db.get(splicing.Closure, n["closure"].id))
    pairs = sorted((s["a"]["fiber"], s["b"]["fiber"]) for s in sheet["splices"])
    assert pairs in ([(1, 2), (2, 3)], [(2, 1), (3, 2)])
    assert all(s["loss_db"] is not None for s in sheet["splices"])


def test_splice_write_errors(db, splice_net):
    n = splice_net
    with pytest.raises(EditError, match="выберите муфту"):
        apply.confirm(db, n["pkg"], cable_id=n["y"].id, port_a_id=None, port_b_id=None, measurement=False,
                      access_node=False, splices=[{"group": 0, "closure_id": None}])
    # Повторная запись тех же сварок не дублирует их
    g = next(x for x in apply.splice_plan(db, n["pkg"], n["y"].id) if x["kind"] == "closure_protocol")
    ch = {k: g[k] for k in ("group", "closure_id", "cable_a_id", "cable_b_id")}
    assert apply._write_splices(db, n["pkg"], [ch], None) == {"created": 2, "already": 0}
    assert apply._write_splices(db, n["pkg"], [ch], None) == {"created": 0, "already": 2}


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
    # Тот же пакет в Word
    files = {"file": ("ИД.docx", sample_docx(scan=False), "application/octet-stream")}
    r = c.post("/api/asbuilt", headers=H, files=files, data={"ocr": "false"})
    assert r.status_code == 200
    pid = r.json()["id"]
    for _ in range(60):
        body = c.get(f"/api/asbuilt/{pid}").json()
        if body["status"] != "processing":
            break
        time.sleep(0.5)
    assert body["status"] == "parsed", body.get("error")
    assert body["summary"]["format"] == "docx" and body["suggest"]["measurement"]["optical_m"] == 2540
    plan = c.get(f"/api/asbuilt/{pid}/splice-plan").json()
    assert {g["kind"] for g in plan} == {"closure_protocol"}
