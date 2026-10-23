"""Файлы для демонстрации строятся по текущей сети и правильно распознаются системой."""
import shutil
import zipfile

from app.services import demo_files, photo_coords, photo_points
from app.services.generator import GeneratorParams, generate_network
from app.services.kml import jobs


def test_demo_files_match_network(db, tmp_path):
    generate_network(db, GeneratorParams(nodes=80, seed=7, buildings_per_operator=4))
    notes = demo_files.build(db, tmp_path)
    assert len(notes) == 7 and (tmp_path / "README.txt").exists()
    # фото колодца: координаты из EXIF, рядом — колодец из подписи
    meta = photo_coords.extract((tmp_path / "foto_kolodec_1.jpg").read_bytes(), "a.jpg")
    assert meta["source"] == "exif"
    code = notes[0].split("колодца ")[1].split(":")[0]
    near = photo_points._nearest(db, meta["lon"], meta["lat"])
    assert near and near["label"] == code and near["distance_m"] < 6
    # муфта по фото: подпись NoteCam читается с точностью
    if shutil.which("tesseract"):
        m = photo_coords.extract((tmp_path / "mufta_po_foto.jpg").read_bytes(), "m.jpg")
        assert m["source"] == "ocr" and m["accuracy_m"] == 6.5
    # Shapefile: импорт из UTM через .prj — 4 новых колодца и 4 участка
    data = (tmp_path / "novaya_ulica_utm39.zip").read_bytes()
    assert {n.rsplit(".", 1)[1] for n in zipfile.ZipFile(tmp_path / "novaya_ulica_utm39.zip").namelist()} >= {
        "shp", "dbf", "prj"}
    job = jobs.create_job(db, "улица.zip", data, None)
    s = jobs.build_preview(db, job)["summary"]
    assert s["nodes_new"] == 4 and s["segments_new"] == 4
