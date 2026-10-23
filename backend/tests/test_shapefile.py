"""Этап 13: Shapefile (чтение/запись), импорт через общий конвейер, системы координат (МСК)."""
import io
import struct
import zipfile

import pytest
import sqlalchemy as sa

from app.models import CrsDef, NetworkNode, NodeKind
from app.services import cards, crs, export, shapefile_io
from app.services.editing import EditError
from app.services.kml import jobs
from app.services.kml.parser import KmlError
from app.services.kml.shp_source import parse_shapefile
from app.services.shapefile_io import Feature

# Две точки и линия между ними в UTM 39N — так выглядит выгрузка из ГИС в метрах
W1 = (52.3300, 55.6700)
W2 = (52.3316, 55.6702)


def utm_wkt(db) -> str:
    return db.scalar(sa.text("SELECT srtext FROM spatial_ref_sys WHERE srid = 32639"))


def to_utm(db, pts):
    return crs.transform(db, pts, crs.WGS84, "EPSG:32639")


def make_zip(layers: dict[str, dict[str, bytes]]) -> bytes:
    files = {}
    for layer in layers.values():
        files.update(layer)
    return shapefile_io.zip_layers(files)


def test_roundtrip_points_lines_cyrillic():
    pts = [Feature("Point", [(1.5, 2.5)], {"NAME": "к.№101", "TYPE": "колодец"}),
           Feature("Point", [(3.0, 4.0)], {"NAME": "оп.7", "TYPE": "опора"})]
    lines = [Feature("LineString", [(0, 0), (10, 5), (20, 0)], {"ID": 5, "LEN": 22.4})]
    files = {**shapefile_io.write_layer("Точки", "Point", pts),
             **shapefile_io.write_layer("Линии", "LineString", lines)}
    layers = {lay.name: lay for lay in shapefile_io.read_layers(files)}
    assert [f.attrs["NAME"] for f in layers["Точки"].features] == ["к.№101", "оп.7"]
    assert layers["Точки"].features[0].coords == [(1.5, 2.5)]
    assert layers["Линии"].features[0].coords == [(0, 0), (10, 5), (20, 0)]
    assert layers["Линии"].features[0].attrs == {"ID": "5", "LEN": "22.4"}   # пишем текстом
    assert layers["Точки"].prj and "WGS" in layers["Точки"].prj


def test_cp1251_without_cpg_and_polygon_and_null_shape():
    files = shapefile_io.write_layer("p", "Point", [Feature("Point", [(1, 1)], {"NAME": "x"})])
    dbf = bytearray(shapefile_io._write_dbf(["NAME"], [{"NAME": "Колодец"}], encoding="cp1251"))
    files["p.dbf"] = bytes(dbf)
    del files["p.cpg"]
    assert shapefile_io.read_layers(files)[0].features[0].attrs["NAME"] == "Колодец"
    # Полигон (тип 5) из двух частей и пустая запись (тип 0)
    ring1, ring2 = [(0, 0), (1, 0), (1, 1), (0, 0)], [(5, 5), (6, 5), (6, 6), (5, 5)]
    pts = ring1 + ring2
    body = struct.pack("<i4d2i", 5, 0, 0, 6, 6, 2, len(pts)) + struct.pack("<2i", 0, 4) + \
        b"".join(struct.pack("<2d", *p) for p in pts)
    null = struct.pack("<i", 0)
    recs = struct.pack(">2i", 1, len(body) // 2) + body + struct.pack(">2i", 2, 2) + null
    shp = struct.pack(">i5ii", 9994, 0, 0, 0, 0, 0, (100 + len(recs)) // 2) + \
        struct.pack("<2i4d4d", 1000, 5, 0, 0, 6, 6, 0, 0, 0, 0) + recs
    layer = shapefile_io.read_layers({"poly.shp": shp})[0]
    assert [f.geom_type for f in layer.features] == ["Polygon", "Polygon"] and layer.skipped == 1
    with pytest.raises(shapefile_io.ShapefileError):
        shapefile_io.read_layers({"x.shp": b"not a shapefile" * 10})


def _utm_package(db, with_prj=True) -> bytes:
    a, b = to_utm(db, [W1, W2])
    wells = shapefile_io.write_layer("Колодцы", "Point", [Feature("Point", [a], {"NAME": "к.№901"}),
                                                          Feature("Point", [b], {"NAME": "к.№902"})], utm_wkt(db))
    duct = shapefile_io.write_layer("Канализация", "LineString", [Feature("LineString", [a, b], {"ID": "1"})],
                                    utm_wkt(db))
    if not with_prj:
        wells.pop("Колодцы.prj")
        duct.pop("Канализация.prj")
    return make_zip({"w": wells, "d": duct})


def test_import_utm_with_prj(db):
    data = _utm_package(db)
    doc = parse_shapefile(db, data)
    assert set(doc.folders()) == {"Колодцы", "Канализация"}
    p = next(pm for pm in doc.placemarks if pm.name == "к.№901")
    assert p.coords[0] == pytest.approx(W1, abs=1e-7)
    job = jobs.create_job(db, "сеть.zip", data, None)
    assert job.kind.value == "shp"
    preview = jobs.build_preview(db, job)
    assert preview["summary"]["nodes_new"] == 2 and preview["summary"]["segments_new"] == 1
    rep = jobs.commit_job(db, job, None)
    assert rep["created"]["nodes"] == 2 and rep["created"]["segments"] == 1
    lon, lat = db.execute(sa.text("SELECT ST_X(geom), ST_Y(geom) FROM network_node WHERE code = 'к.№902'")).one()
    assert (lon, lat) == pytest.approx(W2, abs=1e-6)


def test_import_without_prj_needs_crs(db):
    data = _utm_package(db, with_prj=False)
    with pytest.raises(KmlError, match="не похожи на широту"):
        jobs.create_job(db, "сеть.zip", data, None)
    c = crs.create(db, "UTM 39N (тест)", "EPSG:32639")
    job = jobs.create_job(db, "сеть.zip", data, None, crs_id=c.id)
    assert jobs.build_preview(db, job)["summary"]["nodes_new"] == 2


def test_group_by_type_field(db):
    feats = [Feature("Point", [(52.33 + i * 1e-4, 55.67)], {"NAME": f"т{i}", "ТИП": t})
             for i, t in enumerate(["колодец", "колодец", "опора"])]
    data = make_zip({"a": shapefile_io.write_layer("Объекты", "Point", feats)})
    doc = parse_shapefile(db, data)
    assert set(doc.folders()) == {"колодец", "опора"}
    assert doc.placemarks[0].folder_path == ("Объекты", "колодец")


def test_crs_registry_and_cards(db):
    with pytest.raises(EditError, match="не распознано"):
        crs.create(db, None, "это не система координат")
    c = crs.create(db, None, utm_wkt(db))
    assert c.name.startswith("WGS 84 / UTM zone 39N")
    with pytest.raises(EditError, match="уже есть"):
        crs.create(db, c.name, "EPSG:32639")
    n = NetworkNode(kind=NodeKind.well, code="к.№777", geom=f"SRID=4326;POINT({W1[0]} {W1[1]})")
    db.add(n)
    db.flush()
    db.refresh(n)
    fields = dict((k, v) for k, v in cards.node_card(db, n.id)["fields"])
    assert fields["Координаты (WGS 84)"] == f"{W1[1]:.6f}, {W1[0]:.6f}"
    x, y = to_utm(db, [W1])[0]
    assert fields[c.name] == f"X (север) {y:.2f}  Y (восток) {x:.2f}"
    # Поиск по координатам: в местной системе (X — север, Y — восток) и по широте/долготе
    hits = cards.search(db, f"{y:.2f} {x:.2f}")
    assert hits[0]["type"] == "point" and (hits[0]["lon"], hits[0]["lat"]) == pytest.approx(W1, abs=1e-6)
    hits = cards.search(db, "55.674239, 52.330327")
    assert hits[0]["type"] == "point" and hits[0]["lat"] == pytest.approx(55.674239)


def test_export_shapefile_wgs_and_utm(db):
    from tests.test_localization import _build
    _build(db)
    data = export.to_shapefile(db)
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        names = set(z.namelist())
    assert {"tochki.shp", "trassa.shp", "kabeli.shp", "readme.txt"} <= names
    layers = {lay.name: lay for lay in shapefile_io.read_zip(data)}
    assert len(layers["tochki"].features) == 4 and layers["tochki"].features[0].attrs["KIND"] == "Колодец"
    lon, lat = layers["tochki"].features[0].coords[0]
    assert -180 <= lon <= 180 and -90 <= lat <= 90
    c = CrsDef(name="UTM", definition="EPSG:32639")
    db.add(c)
    db.flush()
    utm = {lay.name: lay for lay in shapefile_io.read_zip(export.to_shapefile(db, crs_def=c))}
    x, _ = utm["tochki"].features[0].coords[0]
    assert x > 100_000 and "UTM zone 39N" in utm["tochki"].prj
    # Выгрузка в метрах читается обратно импортом с пересчётом в WGS 84
    back = parse_shapefile(db, export.to_shapefile(db, crs_def=c))
    assert back.placemarks[0].coords[0] == pytest.approx((lon, lat), abs=1e-7)
