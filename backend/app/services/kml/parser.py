"""Чтение KML/KMZ: папки и объекты (Placemark) с геометрией.

Из файла берутся только структура папок, название, описание и геометрия.
Стили, цвета, LookAt и высота игнорируются (в выгрузках Google Earth это мусор).
Порядок координат в KML — «долгота, широта[, высота]»; так же они хранятся в PostGIS
(x = долгота), поэтому порядок не меняется — меняет его только Leaflet при отображении.
"""
import io
import zipfile
from dataclasses import dataclass, field

from lxml import etree

KML_NS = "http://www.opengis.net/kml/2.2"
# Google Earth иногда пишет пространство имён 2.1 или вовсе без него — ищем по локальному имени
LOCAL = "{*}"


@dataclass
class Placemark:
    name: str | None
    description: str | None
    geom_type: str                      # Point | LineString | Polygon
    coords: list[tuple[float, float]]   # [(lon, lat), ...]
    folder_path: tuple[str, ...]        # цепочка папок от корня
    index: int                          # порядковый номер в файле (для ссылок в отчёте)

    @property
    def folder(self) -> str:
        """Ближайшая папка — по ней сопоставляется тип объекта («Колодцы», «Канализация Таттелеком»)."""
        return self.folder_path[-1] if self.folder_path else "(корень)"


@dataclass
class KmlDocument:
    name: str | None
    placemarks: list[Placemark] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def folders(self) -> dict[str, dict]:
        """Сводка по папкам: число объектов каждого типа геометрии."""
        out: dict[str, dict] = {}
        for p in self.placemarks:
            f = out.setdefault(p.folder, {"name": p.folder, "path": " / ".join(p.folder_path), "count": 0,
                                          "types": {}})
            f["count"] += 1
            f["types"][p.geom_type] = f["types"].get(p.geom_type, 0) + 1
        return out


class KmlError(ValueError):
    pass


def _text(el, tag: str) -> str | None:
    child = el.find(f"{LOCAL}{tag}")
    if child is None or child.text is None:
        return None
    value = " ".join(child.text.split())
    return value or None


def _coords(text: str | None) -> list[tuple[float, float]]:
    """'lon,lat,alt lon,lat,alt ...' → [(lon, lat), ...]."""
    result = []
    for chunk in (text or "").split():
        parts = chunk.split(",")
        if len(parts) >= 2:
            result.append((float(parts[0]), float(parts[1])))
    return result


def _geometries(pm) -> list[tuple[str, list[tuple[float, float]]]]:
    """Все геометрии объекта; MultiGeometry раскладывается на части."""
    out = []
    for tag in ("Point", "LineString", "LinearRing", "Polygon"):
        for g in pm.iter(f"{LOCAL}{tag}"):
            if tag == "LinearRing" and g.getparent() is not None and \
                    etree.QName(g.getparent()).localname in ("outerBoundaryIs", "innerBoundaryIs"):
                continue  # кольца полигона берём через сам Polygon
            if tag == "Polygon":
                ring = g.find(f"{LOCAL}outerBoundaryIs/{LOCAL}LinearRing/{LOCAL}coordinates")
                coords = _coords(ring.text if ring is not None else None)
            else:
                node = g.find(f"{LOCAL}coordinates")
                coords = _coords(node.text if node is not None else None)
            kind = "LineString" if tag == "LinearRing" else tag
            if coords:
                out.append((kind, coords))
    return out


def read_kml_bytes(data: bytes) -> bytes:
    """KMZ — это zip-архив с doc.kml внутри; KML возвращается как есть."""
    if data[:2] == b"PK":
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            names = [n for n in z.namelist() if n.lower().endswith(".kml")]
            if not names:
                raise KmlError("В архиве KMZ нет файла .kml")
            main = "doc.kml" if "doc.kml" in names else names[0]
            return z.read(main)
    return data


def parse_kml(data: bytes) -> KmlDocument:
    try:
        # resolve_entities=False, no_network — защита от XXE-атак через внешние сущности
        parser = etree.XMLParser(resolve_entities=False, no_network=True, huge_tree=False, recover=False)
        root = etree.fromstring(read_kml_bytes(data), parser=parser)
    except (etree.XMLSyntaxError, zipfile.BadZipFile) as exc:
        raise KmlError(f"Файл не похож на KML/KMZ: {exc}") from None
    if etree.QName(root).localname != "kml":
        raise KmlError("Корневой элемент файла — не <kml>")

    doc_el = root.find(f"{LOCAL}Document")
    doc = KmlDocument(name=_text(doc_el, "name") if doc_el is not None else None)
    counter = 0

    def walk(el, path: tuple[str, ...]):
        nonlocal counter
        for child in el:
            if not isinstance(child.tag, str):
                continue  # комментарии и инструкции обработки
            local = etree.QName(child).localname
            if local in ("Folder", "Document"):
                name = _text(child, "name")
                # Документ-обёртку (имя файла) в путь не включаем
                walk(child, path if local == "Document" else (*path, name or "(без имени)"))
            elif local == "Placemark":
                geoms = _geometries(child)
                if not geoms:
                    doc.errors.append(f"Объект «{_text(child, 'name')}» без геометрии — пропущен")
                    continue
                for kind, coords in geoms:
                    counter += 1
                    doc.placemarks.append(Placemark(
                        name=_text(child, "name"), description=_text(child, "description"),
                        geom_type=kind, coords=coords, folder_path=path, index=counter))

    walk(root, ())
    return doc
