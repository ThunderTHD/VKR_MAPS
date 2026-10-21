"""Синтетический KML «как из Google Earth» для тестов.

Повторяет особенности реальных выгрузок (структура папок, стили, LookAt, разнобой в
названиях, линии, нарисованные в обратную сторону, концы мимо колодцев), но координаты
и коды вымышлены. Реальные схемы в репозиторий не попадают.
"""
import io
import math
import zipfile

LAT0, LON0 = 55.10, 50.10  # условное место, далеко от реальной сети
M_LAT = 1 / 111_320
M_LON = 1 / (111_320 * math.cos(math.radians(LAT0)))


def pt(x_m: float, y_m: float) -> tuple[float, float]:
    """Локальные метры → (lon, lat)."""
    return (LON0 + x_m * M_LON, LAT0 + y_m * M_LAT)


# Колодцы: код → положение (метры)
WELLS = {
    "301": (0, 0), "348": (150, 0), "498": (300, 0), "118": (300, 150), "02-17": (450, 0),
    "31-16": (0, 300), "508": (150, 300), "651-А": (300, 300), "485": (450, 300), "440": (450, 450),
}


def _coords(points) -> str:
    return " ".join(f"{lon:.8f},{lat:.8f},0" for lon, lat in points)


def _placemark(name: str | None, geom: str) -> str:
    n = f"<name>{name}</name>" if name is not None else ""
    return (f"<Placemark>{n}<LookAt><longitude>{LON0}</longitude><latitude>{LAT0}</latitude></LookAt>"
            f"<styleUrl>#msn_ylw-pushpin21</styleUrl>{geom}</Placemark>")


def point(name, x, y) -> str:
    return _placemark(name, f"<Point><coordinates>{_coords([pt(x, y)])}</coordinates></Point>")


def line(name, *xy) -> str:
    return _placemark(name, f"<LineString><tessellate>1</tessellate><coordinates>"
                            f"{_coords([pt(x, y) for x, y in xy])}</coordinates></LineString>")


def folder(name: str, *items: str) -> str:
    return f"<Folder><name>{name}</name>{''.join(items)}</Folder>"


def sample_kml() -> bytes:
    wells = [point(f"Колодец Таттелеком №{c}" if c not in ("02-17", "31-16") else
                   ("Колодец ТВТ 02-17" if c == "02-17" else "Колодец 31-16"), *xy)
             for c, xy in WELLS.items()]
    tattelecom = [
        line("к.№301-  к.№348", (0.4, 0.2), (75, 3), (149.7, -0.3)),        # разнобой пробелов, концы мимо на ~0,5 м
        line("к.№348 - к.№498", (300, 0), (225, -2), (150, 0)),              # нарисована в обратную сторону
        line("к.№498 - к.№118", (300, 0), (300, 150)),
        line("к.№498 - 57110", (300, 0), (380, -60), (420, -120)),           # конец — новая площадка 57110
        line("к.31-16 - к.№508 - 57177", (0, 300), (150, 300.5), (150, 420)),  # проходит через 508 — разрезать
        line("к.№508 - к.№651-А", (150, 300), (300, 300)),
        line("к.№445 - к.№440", (450, 301.5), (450, 450)),                   # опечатка: на деле от 485
        line("Untitled Path", (300, 300), (450, 300)),
        line("КамАЗ", (450, 450), (600, 470), (700, 520)),
        line("к.№118 - к.№02-17", (300, 150), (450, 0)),
    ]
    vimpelcom = [line("К.02-17 - К.53-01", (450, 0), (600, 0), (750, 0), (900, 0), (1150, 0))]
    nec = [line("57110 - 56760", (420, -120), (1500, -900))]  # прямая на ~1,3 км — назначение неизвестно
    closures = [point('Муфта  столб "Электротранспорт"', 150.4, 0.3)]
    body = folder(
        "ВОЛС Тестоград",
        folder("Муфты", *closures),
        folder("Колодцы", *wells),
        folder("Канализация Таттелеком", *tattelecom),
        folder("Канализации ВымпелКом", *vimpelcom),
        folder("NEC", *nec),
    )
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<kml xmlns="http://www.opengis.net/kml/2.2">'
            f"<Document><name>Тестоград.kml</name><Style id='s'/>{body}</Document></kml>").encode()


def sample_kmz() -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("doc.kml", sample_kml())
    return buf.getvalue()
