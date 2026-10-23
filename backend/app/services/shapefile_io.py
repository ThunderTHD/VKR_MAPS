"""Чтение и запись Shapefile (ESRI) без внешних библиотек.

Shapefile — набор файлов с общим именем: .shp (геометрия), .shx (индекс), .dbf (атрибуты,
формат dBASE), .prj (система координат в WKT), .cpg (кодировка атрибутов). Его выгружают
5Gis, QGIS, MapInfo, AutoCAD Map. Формат открытый (ESRI Shapefile Technical Description, 1998)
и простой, поэтому читаем и пишем его сами — не нужна GDAL и сборка нативных библиотек.

Поддерживаются геометрии: точка, мультиточка, линия (PolyLine), полигон и их варианты с Z/M —
берутся только X, Y. Каждая часть линии/полигона — отдельный объект.
"""
import io
import struct
import zipfile
from dataclasses import dataclass, field
from datetime import date

POINT_TYPES, MULTIPOINT_TYPES, LINE_TYPES, POLYGON_TYPES = {1, 11, 21}, {8, 18, 28}, {3, 13, 23}, {5, 15, 25}
WGS84_WKT = ('GEOGCS["GCS_WGS_1984",DATUM["D_WGS_1984",SPHEROID["WGS_1984",6378137.0,298.257223563]],'
             'PRIMEM["Greenwich",0.0],UNIT["Degree",0.0174532925199433]]')


class ShapefileError(ValueError):
    pass


@dataclass
class Feature:
    geom_type: str                       # Point | LineString | Polygon
    coords: list[tuple[float, float]]
    attrs: dict[str, object]


@dataclass
class Layer:
    name: str
    features: list[Feature] = field(default_factory=list)
    prj: str | None = None
    fields: list[str] = field(default_factory=list)
    encoding: str = "utf-8"
    skipped: int = 0                     # пустые и неподдерживаемые геометрии


# ------------------------------------------------------------------ чтение


def _read_shp(data: bytes) -> list[list[tuple[str, list[tuple[float, float]]]] | None]:
    """Геометрии по номерам записей: список частей [(тип, точки)]; None — пустая/неподдерживаемая."""
    if len(data) < 100 or struct.unpack(">i", data[:4])[0] != 9994:
        raise ShapefileError("Это не файл .shp (неверная сигнатура)")
    out: list = []
    pos = 100
    while pos + 8 <= len(data):
        _, length = struct.unpack(">2i", data[pos:pos + 8])
        rec = data[pos + 8:pos + 8 + length * 2]
        pos += 8 + length * 2
        if len(rec) < 4:
            break
        st = struct.unpack("<i", rec[:4])[0]
        if st in POINT_TYPES:
            x, y = struct.unpack("<2d", rec[4:20])
            out.append([("Point", [(x, y)])])
        elif st in MULTIPOINT_TYPES:
            n = struct.unpack("<i", rec[36:40])[0]
            pts = [struct.unpack("<2d", rec[40 + 16 * i:56 + 16 * i]) for i in range(n)]
            out.append([("Point", [p]) for p in pts])
        elif st in LINE_TYPES | POLYGON_TYPES:
            nparts, npoints = struct.unpack("<2i", rec[36:44])
            parts = list(struct.unpack(f"<{nparts}i", rec[44:44 + 4 * nparts])) + [npoints]
            base = 44 + 4 * nparts
            pts = [struct.unpack("<2d", rec[base + 16 * i:base + 16 * i + 16]) for i in range(npoints)]
            kind = "LineString" if st in LINE_TYPES else "Polygon"
            out.append([(kind, pts[parts[i]:parts[i + 1]]) for i in range(nparts) if parts[i + 1] > parts[i]])
        else:          # 0 — пустая геометрия; мультипатчи и прочее не поддерживаем
            out.append(None)
    return out


def _read_dbf(data: bytes, encoding: str) -> tuple[list[str], list[dict]]:
    if len(data) < 32:
        return [], []
    nrec, hlen, rlen = struct.unpack("<IHH", data[4:12])
    fields = []
    pos = 32
    while pos < hlen - 1 and data[pos] != 0x0D:
        raw = data[pos:pos + 32]
        name = raw[:11].split(b"\x00")[0].decode(encoding, errors="replace").strip()
        fields.append((name, chr(raw[11]), raw[16], raw[17]))
        pos += 32
    rows = []
    for i in range(nrec):
        rec = data[hlen + i * rlen:hlen + (i + 1) * rlen]
        if not rec or rec[:1] == b"*":       # удалённая запись
            rows.append(None)
            continue
        off, row = 1, {}
        for name, ftype, flen, dec in fields:
            raw = rec[off:off + flen]
            off += flen
            text = raw.decode(encoding, errors="replace").strip()
            if ftype in "NF":
                try:
                    row[name] = (float(text) if dec or "." in text else int(text)) if text else None
                except ValueError:
                    row[name] = None
            elif ftype == "L":
                row[name] = text.upper() in ("Y", "T") if text else None
            elif ftype == "D" and len(text) == 8 and text.isdigit():
                row[name] = f"{text[:4]}-{text[4:6]}-{text[6:]}"
            else:
                row[name] = text or None
        rows.append(row)
    return [f[0] for f in fields], rows


def _guess_encoding(cpg: str | None, dbf: bytes) -> str:
    """Кодировка атрибутов: из .cpg; если его нет — UTF-8, если текст в ней читается, иначе CP1251."""
    if cpg:
        c = cpg.strip().lower().replace("ansi ", "cp")
        if c in ("1251", "cp1251", "windows-1251"):
            return "cp1251"
        if c in ("utf-8", "utf8", "65001"):
            return "utf-8"
        if c in ("866", "cp866"):
            return "cp866"
    if len(dbf) > 29 and dbf[29] in (0xC9, 0x57):        # байт кодовой страницы dBASE: 1251
        return "cp1251"
    try:
        dbf.decode("utf-8")
        return "utf-8"
    except UnicodeDecodeError:
        return "cp1251"


def read_layers(files: dict[str, bytes]) -> list[Layer]:
    """Слои из набора файлов {имя: содержимое} (имена — без каталогов, любой регистр)."""
    by_base: dict[str, dict[str, bytes]] = {}
    for name, data in files.items():
        base, _, ext = name.replace("\\", "/").rsplit("/", 1)[-1].rpartition(".")
        by_base.setdefault(base, {})[ext.lower()] = data
    layers = []
    for base, parts in sorted(by_base.items()):
        if "shp" not in parts:
            continue
        cpg = parts.get("cpg", b"").decode("ascii", errors="ignore") or None
        enc = _guess_encoding(cpg, parts.get("dbf", b""))
        fields, rows = _read_dbf(parts["dbf"], enc) if "dbf" in parts else ([], [])
        geoms = _read_shp(parts["shp"])
        layer = Layer(name=base, prj=parts.get("prj", b"").decode("utf-8", errors="ignore").strip() or None,
                      fields=fields, encoding=enc)
        for i, g in enumerate(geoms):
            attrs = rows[i] if i < len(rows) and rows[i] is not None else {}
            if not g:
                layer.skipped += 1
                continue
            for kind, coords in g:
                layer.features.append(Feature(kind, [(float(x), float(y)) for x, y in coords], dict(attrs)))
        layers.append(layer)
    if not layers:
        raise ShapefileError("В наборе нет файла .shp")
    return layers


def read_zip(data: bytes) -> list[Layer]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            files = {n: z.read(n) for n in z.namelist()
                     if not n.endswith("/") and n.lower().rsplit(".", 1)[-1] in ("shp", "dbf", "prj", "cpg", "shx")}
    except zipfile.BadZipFile as exc:
        raise ShapefileError("Архив повреждён") from exc
    return read_layers(files)


def is_shapefile_zip(data: bytes) -> bool:
    if data[:2] != b"PK":
        return False
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as z:
            return any(n.lower().endswith(".shp") for n in z.namelist())
    except zipfile.BadZipFile:
        return False


# ------------------------------------------------------------------ запись


def _bbox(points):
    xs, ys = [p[0] for p in points], [p[1] for p in points]
    return (min(xs), min(ys), max(xs), max(ys)) if points else (0.0, 0.0, 0.0, 0.0)


def _write_shp(shape_type: int, geoms: list[list[tuple[float, float]]]) -> tuple[bytes, bytes]:
    recs = []
    for g in geoms:
        if shape_type == 1:
            body = struct.pack("<i2d", 1, *g[0])
        else:
            bb = _bbox(g)
            body = struct.pack("<i4d2ii", 3, *bb, 1, len(g), 0) + b"".join(struct.pack("<2d", *p) for p in g)
        recs.append(body)
    all_pts = [p for g in geoms for p in g]
    bb = _bbox(all_pts)

    def header(total_bytes: int) -> bytes:
        return (struct.pack(">i5ii", 9994, 0, 0, 0, 0, 0, total_bytes // 2) +
                struct.pack("<2i4d4d", 1000, shape_type, *bb, 0, 0, 0, 0))
    shp, shx = io.BytesIO(), io.BytesIO()
    offset = 100
    for i, body in enumerate(recs):
        shp.write(struct.pack(">2i", i + 1, len(body) // 2) + body)
        shx.write(struct.pack(">2i", offset // 2, len(body) // 2))
        offset += 8 + len(body)
    shp_bytes = header(100 + shp.tell()) + shp.getvalue()
    shx_bytes = header(100 + shx.tell()) + shx.getvalue()
    return shp_bytes, shx_bytes


def _write_dbf(fields: list[str], rows: list[dict], encoding: str = "utf-8") -> bytes:
    """Все поля — текстовые (C), длина по самому длинному значению (до 254 байт)."""
    names = list(fields)
    vals = [[("" if r.get(f) is None else str(r.get(f))).encode(encoding)[:254] for f in fields] for r in rows]
    lens = [max([1] + [len(v[i]) for v in vals]) for i in range(len(fields))]
    hlen = 32 + 32 * len(fields) + 1
    rlen = 1 + sum(lens)
    today = date.today()
    out = io.BytesIO()
    out.write(struct.pack("<4BIHH20x", 3, today.year - 1900, today.month, today.day, len(rows), hlen, rlen))
    for name, ln in zip(names, lens, strict=True):
        out.write(name.encode(encoding, errors="replace")[:10].ljust(11, b"\x00") + b"C" + b"\x00" * 4 +
                  bytes([ln, 0]) + b"\x00" * 14)
    out.write(b"\x0d")
    for v in vals:
        out.write(b" " + b"".join(x.ljust(ln, b" ") for x, ln in zip(v, lens, strict=True)))
    out.write(b"\x1a")
    return out.getvalue()


def write_layer(name: str, geom_type: str, features: list[Feature], prj: str = WGS84_WKT) -> dict[str, bytes]:
    """Слой (только точки или только линии) → {имя.shp: …, .shx, .dbf, .prj, .cpg}."""
    shape_type = 1 if geom_type == "Point" else 3
    fields = list(dict.fromkeys(k for f in features for k in f.attrs))
    shp, shx = _write_shp(shape_type, [f.coords for f in features])
    return {f"{name}.shp": shp, f"{name}.shx": shx, f"{name}.dbf": _write_dbf(fields, [f.attrs for f in features]),
            f"{name}.prj": prj.encode("utf-8"), f"{name}.cpg": b"UTF-8"}


def zip_layers(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()
