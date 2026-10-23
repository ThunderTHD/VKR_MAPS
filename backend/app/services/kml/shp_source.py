"""Shapefile как источник для того же импорта, что и KML (этап 13).

Слои Shapefile превращаются в «папки», объекты — в Placemark: дальше работают те же
сопоставление папок с типами объектов, привязка концов линий к колодцам, предпросмотр и
запись (этап 4). Координаты пересчитываются в WGS 84 по .prj или по системе, выбранной
пользователем (МСК).

Если в слое есть поле «тип» (TYPE, ТИП, LAYER, СЛОЙ, KIND, CATEGORY) с небольшим числом
значений — объекты раскладываются по подпапкам по этому полю: 5Gis часто выгружает всё в
один слой, а тип объекта хранит в атрибуте.
"""
from sqlalchemy.orm import Session

from app.services import crs, shapefile_io
from app.services.editing import EditError
from app.services.kml.parser import KmlDocument, KmlError, Placemark

NAME_FIELDS = ("name", "название", "наименов", "nazv", "label", "подпись", "text", "номер", "number", "num", "code",
               "код")
TYPE_FIELDS = ("type", "тип", "layer", "слой", "kind", "category", "категория", "класс", "class")
MAX_GROUPS = 20


def _field(fields: list[str], keys: tuple[str, ...]) -> str | None:
    low = {f.lower(): f for f in fields}
    for k in keys:
        for lf, f in low.items():
            if lf == k or lf.startswith(k):
                return f
    return None


def _describe(attrs: dict, skip: set[str]) -> str | None:
    parts = [f"{k}: {v}" for k, v in attrs.items() if k not in skip and v not in (None, "")]
    return "; ".join(parts)[:500] or None


def parse_shapefile(db: Session, data: bytes, crs_definition: str | None = None) -> KmlDocument:
    try:
        layers = shapefile_io.read_zip(data)
    except shapefile_io.ShapefileError as exc:
        raise KmlError(str(exc)) from None
    doc = KmlDocument(name=layers[0].name if len(layers) == 1 else "Shapefile")
    index = 0
    for layer in layers:
        definition = layer.prj or crs_definition
        if layer.prj and crs_definition and not crs.is_wgs84(layer.prj):
            definition = layer.prj      # .prj в архиве точнее выбора по списку
        # Пересчёт всех точек слоя одним запросом
        flat = [p for f in layer.features for p in f.coords]
        try:
            wgs = crs.to_wgs84(db, flat, definition)
        except EditError as exc:
            raise KmlError(f"Слой «{layer.name}»: {exc}") from None
        name_f = _field(layer.fields, NAME_FIELDS)
        type_f = _field(layer.fields, TYPE_FIELDS)
        if type_f:
            values = {f.attrs.get(type_f) for f in layer.features}
            if not 1 < len(values) <= MAX_GROUPS:
                type_f = None
        pos = 0
        for f in layer.features:
            coords = wgs[pos:pos + len(f.coords)]
            pos += len(f.coords)
            folder = (layer.name, str(f.attrs.get(type_f) or "без типа")) if type_f else (layer.name,)
            name = f.attrs.get(name_f) if name_f else None
            doc.placemarks.append(Placemark(
                name=str(name).strip() if name not in (None, "") else None,
                description=_describe(f.attrs, {name_f} if name_f else set()),
                geom_type=f.geom_type, coords=coords, folder_path=folder, index=index))
            index += 1
        if layer.skipped:
            doc.errors.append(f"Слой «{layer.name}»: пропущено объектов без геометрии — {layer.skipped}")
    if not doc.placemarks:
        raise KmlError("В Shapefile нет объектов с геометрией")
    return doc
