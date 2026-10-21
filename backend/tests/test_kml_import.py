"""Импорт KML: разбор названий, привязка, предпросмотр, запись, повторный импорт, API."""
import pytest
import sqlalchemy as sa

from app.models import Closure, KmlFolderRule, NetworkNode, Owner, RouteSegment, UnclassifiedFeature
from app.services.kml.importer import FolderMapping, commit_plan, plan_import, suggest_mapping
from app.services.kml.names import parse_line_name, parse_point_name, split_line_name
from app.services.kml.parser import KmlError, parse_kml
from tests.kml_factory import sample_kml, sample_kmz

# ------------------------------------------------------------------ названия -----


@pytest.mark.parametrize("text, kind, code", [
    ("Колодец Таттелеком №118", "well", "к.№118"),
    ("Колодец ТВТ 02-17", "well", "к.№02-17"),
    ("Колодец 31-16", "well", "к.№31-16"),
    ("Колодец Таттелеком №651-а", "well", "к.№651-А"),
    ("к.№348", "well", "к.№348"),
    ("к.461", "well", "к.№461"),
    ("К.02-17", "well", "к.№02-17"),
    ("К. № 53-01", "well", "к.№53-01"),
    ("57121", "site", "57121"),
    ("Мира 15Б", "name", None),
    ("ТатАИСнефть", "name", None),
])
def test_parse_point_name(text, kind, code):
    ep = parse_point_name(text)
    assert (ep.kind, ep.code) == (kind, code)


def test_owner_hint_from_point_name():
    assert parse_point_name("Колодец Таттелеком №118").owner_hint == "Таттелеком"


@pytest.mark.parametrize("text, parts", [
    ("к.№348 - к.№498", ["к.№348", "к.№498"]),
    ("к.№301-  к.№348", ["к.№301", "к.№348"]),       # пробел только после дефиса
    ("55135 -57100", ["55135", "57100"]),             # пробел только перед дефисом
    ("к.№508 - к.№651-А", ["к.№508", "к.№651-А"]),    # дефис внутри кода — не разделитель
    ("К.02-17 - К.53-01", ["К.02-17", "К.53-01"]),
    ("к.31-16 - к.№508 - 57177", ["к.31-16", "к.№508", "57177"]),
    ("Форт-Диалог", ["Форт-Диалог"]),
    ("Untitled Path", []),
    ("", []),
])
def test_split_line_name(text, parts):
    assert split_line_name(text) == parts


def test_parse_line_name_three_points():
    eps = parse_line_name("к.31-16 - к.№508 - 57177")
    assert [(e.kind, e.code) for e in eps] == [("well", "к.№31-16"), ("well", "к.№508"), ("site", "57177")]
    assert parse_line_name("КамАЗ") == []  # одно имя — концов нет


# ------------------------------------------------------------------ парсер -----

def test_parse_kml_structure():
    doc = parse_kml(sample_kml())
    folders = doc.folders()
    assert set(folders) == {"Муфты", "Колодцы", "Канализация Таттелеком", "Канализации ВымпелКом", "NEC"}
    assert folders["Колодцы"]["types"] == {"Point": 10}
    assert folders["Канализация Таттелеком"]["types"] == {"LineString": 10}
    first_line = next(p for p in doc.placemarks if p.geom_type == "LineString")
    lon, lat = first_line.coords[0]
    assert 50 < lon < 51 and 55 < lat < 56  # порядок: долгота, широта


def test_kmz_supported():
    assert len(parse_kml(sample_kmz()).placemarks) == len(parse_kml(sample_kml()).placemarks)


@pytest.mark.parametrize("data", [b"not xml at all", b"<html><body/></html>", b"PK\x03\x04broken"])
def test_bad_files_rejected(data):
    with pytest.raises(KmlError):
        parse_kml(data)


def test_external_entities_not_resolved(tmp_path):
    """XXE: внешняя сущность в файле не должна прочитать файл с сервера."""
    secret = tmp_path / "secret.txt"
    secret.write_text("СЕКРЕТ")
    evil = (f'<?xml version="1.0"?><!DOCTYPE kml [<!ENTITY x SYSTEM "file://{secret}">]>'
            f'<kml xmlns="http://www.opengis.net/kml/2.2"><Document><Placemark><name>&x;</name>'
            f'<Point><coordinates>50,55,0</coordinates></Point></Placemark></Document></kml>').encode()
    doc = parse_kml(evil)
    assert all("СЕКРЕТ" not in (p.name or "") for p in doc.placemarks)


# ------------------------------------------------------------------ план и запись -----

@pytest.fixture()
def clean(db):
    db.execute(sa.text("TRUNCATE network_node, route_segment, closure, unclassified_feature, kml_folder_rule, "
                       "import_job RESTART IDENTITY CASCADE"))
    return db


def test_suggest_mapping(clean):
    m = suggest_mapping(clean, parse_kml(sample_kml()))
    owners = {o.short_name: o.id for o in clean.scalars(sa.select(Owner))}
    assert m["Колодцы"].target == "node" and m["Колодцы"].node_kind == "well"
    assert m["Муфты"].target == "closure"
    tt = m["Канализация Таттелеком"]
    assert tt.target == "segment" and tt.owner_id == owners["Таттелеком"]
    assert m["Канализации ВымпелКом"].owner_id == owners["ВымпелКом"]
    assert m["NEC"].target == "unclassified"


def _plan(db, tol=10.0):
    doc = parse_kml(sample_kml())
    mapping = suggest_mapping(db, doc)
    return doc, mapping, plan_import(db, doc, mapping, tol)


def test_plan_binds_lines_to_wells(clean):
    _, _, plan = _plan(clean)
    by_name = {}
    for s in plan.segments:
        by_name.setdefault(s.name, []).append(s)
    code = lambda key: plan.nodes[key].code  # noqa: E731
    s = by_name["к.№301- к.№348"][0]  # пробелы в названии нормализуются
    assert {code(s.a), code(s.b)} == {"к.№301", "к.№348"}
    # Нарисована от 498 к 348, а названа «348 - 498» — привязка всё равно верная
    s = by_name["к.№348 - к.№498"][0]
    assert {code(s.a), code(s.b)} == {"к.№348", "к.№498"}


def test_plan_splits_three_point_line(clean):
    _, _, plan = _plan(clean)
    parts = [s for s in plan.segments if s.name == "к.31-16 - к.№508 - 57177"]
    assert len(parts) == 2
    codes = [{plan.nodes[p.a].code, plan.nodes[p.b].code} for p in parts]
    assert {"к.№31-16", "к.№508"} in codes and {"к.№508", "57177"} in codes


def test_plan_detects_typo_in_name(clean):
    """«к.№445 - к.№440» начинается у колодца 485 — система привязывает к 485 и предупреждает."""
    _, _, plan = _plan(clean)
    w = [w for w in plan.warnings if w.code == "code_mismatch"]
    assert w and "к.№445" in w[0].message and "к.№485" in w[0].message


def test_plan_auto_nodes_and_warnings(clean):
    _, _, plan = _plan(clean)
    auto = {n.code or n.name for n in plan.nodes.values() if n.status == "auto"}
    assert {"57110", "57177", "к.№53-01"} <= auto
    codes = {w.code for w in plan.warnings}
    assert {"untitled", "unparsed", "auto_node"} <= codes
    assert len(plan.unclassified) == 1  # папка NEC


def _job(db) -> int:
    from app.models import ImportJob, ImportKind

    job = ImportJob(kind=ImportKind.kml, filename="t.kml")
    db.add(job)
    db.flush()
    return job.id


def test_commit_and_reimport_without_duplicates(clean):
    doc, mapping, plan = _plan(clean)
    created = commit_plan(clean, plan, job_id=_job(clean), user_id=None, mapping=mapping)
    assert created["segments"] == plan.summary()["segments_new"] > 0
    assert created["closures"] == 1
    n_nodes = clean.scalar(sa.select(sa.func.count(NetworkNode.id)))
    n_segs = clean.scalar(sa.select(sa.func.count(RouteSegment.id)))
    # Концы участков точно совпадают с узлами (топология без зазоров)
    gaps = clean.execute(sa.text("""
        SELECT count(*) FROM route_segment s JOIN network_node a ON a.id = s.node_a_id
        JOIN network_node b ON b.id = s.node_b_id
        WHERE NOT ST_Equals(ST_StartPoint(s.geom), a.geom) OR NOT ST_Equals(ST_EndPoint(s.geom), b.geom)""")
    ).scalar_one()
    assert gaps == 0
    # Сопоставление папок запомнено
    assert clean.scalar(sa.select(sa.func.count(KmlFolderRule.id))) == 5

    _, mapping2, plan2 = _plan(clean)
    assert plan2.summary()["segments_new"] == 0 and plan2.summary()["nodes_new"] == 0
    again = commit_plan(clean, plan2, job_id=_job(clean), user_id=None, mapping=mapping2)
    assert again == {"nodes": 0, "segments": 0, "closures": 0, "unclassified": 0, "nodes_updated": 0}
    assert clean.scalar(sa.select(sa.func.count(NetworkNode.id))) == n_nodes
    assert clean.scalar(sa.select(sa.func.count(RouteSegment.id))) == n_segs
    assert clean.scalar(sa.select(sa.func.count(UnclassifiedFeature.id))) == 1
    assert clean.scalar(sa.select(sa.func.count(Closure.id))) == 1


def test_skip_folder(clean):
    doc = parse_kml(sample_kml())
    mapping = suggest_mapping(clean, doc)
    mapping["NEC"] = FolderMapping("skip")
    plan = plan_import(clean, doc, mapping)
    assert plan.unclassified == [] and plan.skipped == 1


def test_tolerance_affects_binding(clean):
    """При допуске 0,1 м линия «к.№301-  к.№348» (концы мимо на ~0,5 м) привязывается только по коду."""
    _, _, plan = _plan(clean, tol=0.1)
    s = next(s for s in plan.segments if s.name == "к.№301- к.№348")
    assert {plan.nodes[s.a].code, plan.nodes[s.b].code} == {"к.№301", "к.№348"}


# ------------------------------------------------------------------ API -----

@pytest.fixture()
def admin_client(client, db):
    from app.core.db import SessionLocal
    from app.core.security import hash_password
    from app.models import AppUser, Role

    with SessionLocal() as s:
        s.execute(sa.text("TRUNCATE network_node, route_segment, closure, unclassified_feature, kml_folder_rule, "
                          "import_job RESTART IDENTITY CASCADE"))
        for login, role in (("k_admin", Role.admin), ("k_viewer", Role.viewer)):
            if not s.scalar(sa.select(AppUser).where(AppUser.login == login)):
                s.add(AppUser(login=login, full_name=login, role=role, password_hash=hash_password("пароль-1")))
        s.commit()
    client.post("/api/auth/login", json={"login": "k_admin", "password": "пароль-1"})
    client.headers["X-Requested-With"] = "fetch"
    return client


def test_api_import_flow(admin_client):
    c = admin_client
    kml_type = "application/vnd.google-earth.kml+xml"
    r = c.post("/api/imports/kml", files={"file": ("Тестоград.kml", sample_kml(), kml_type)})
    assert r.status_code == 200, r.text
    body = r.json()
    job_id = body["job"]["id"]
    assert body["summary"]["segments_new"] > 0
    assert body["geojson"]["type"] == "FeatureCollection"
    assert {f["name"] for f in body["folders"]} >= {"Колодцы", "NEC"}

    # Меняем сопоставление: NEC — пропустить
    mapping = body["mapping"]
    mapping["NEC"] = {"target": "skip"}
    r = c.post(f"/api/imports/{job_id}/preview", json={"mapping": mapping, "tolerance_m": 10})
    assert r.json()["summary"]["unclassified"] == 0

    r = c.post(f"/api/imports/{job_id}/commit")
    assert r.status_code == 200 and r.json()["report"]["created"]["segments"] > 0
    assert c.post(f"/api/imports/{job_id}/commit").status_code == 409  # дважды нельзя
    assert c.get("/api/imports").json()[0]["status"] == "committed"
    layer = c.get("/api/map/layers/segments").json()
    assert len(layer["features"]) == r.json()["report"]["created"]["segments"]


def test_api_import_rejects_garbage(admin_client):
    r = admin_client.post("/api/imports/kml", files={"file": ("x.kml", b"garbage", "text/plain")})
    assert r.status_code == 400


def test_api_import_admin_only(admin_client):
    c = admin_client
    c.post("/api/auth/login", json={"login": "k_viewer", "password": "пароль-1"})
    r = c.post("/api/imports/kml", files={"file": ("t.kml", sample_kml(), "text/xml")})
    assert r.status_code == 403
