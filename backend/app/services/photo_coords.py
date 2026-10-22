"""Координаты места съёмки по фотографии: EXIF или распознанная подпись на кадре.

Монтажники фотографируют колодцы, опоры и муфты. Камера телефона пишет координаты в
EXIF (теги GPSLatitude/GPSLongitude), если в настройках камеры включено «сохранять
местоположение». Часто фото проходят через мессенджеры, которые EXIF вырезают, — на этот
случай используются приложения «GPS-камера» (GPS Map Camera, Timestamp Camera и т. п.),
впечатывающие координаты прямо в кадр. Такую подпись распознаём OCR (Tesseract).

Поддерживаемые записи координат на кадре:
  55.701234, 52.345678 · 55,701234° N 52,345678° E · Lat 55.701234 Long 52.345678 ·
  Широта: 55.701234 Долгота: 52.345678 · 55°42'04.4" N 52°20'44.4" E.
"""
import contextlib
import io
import re
from datetime import datetime
from zoneinfo import ZoneInfo

from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.config import get_settings

GPS_IFD, EXIF_IFD = 0x8825, 0x8769
DATETIME_ORIGINAL = 36867
IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".tif", ".tiff"}

NUM = r"(-?\d{1,3}(?:[.,]\d{3,}))"
HEMI_LAT, HEMI_LON = r"([NSСЮ])", r"([EWВЗ])"
PATTERNS = [
    # Lat 55.701234 Long 52.345678 / Широта: … Долгота: …
    re.compile(rf"(?:lat(?:itude)?|шир(?:ота)?)\s*[:=]?\s*{NUM}\s*°?\s*{HEMI_LAT}?.{{0,40}}?"
               rf"(?:lon(?:g(?:itude)?)?|lng|долг(?:ота)?)\s*[:=]?\s*{NUM}\s*°?\s*{HEMI_LON}?", re.I | re.S),
    # 55.701234° N, 52.345678° E  /  55.701234, 52.345678
    re.compile(rf"{NUM}\s*°?\s*{HEMI_LAT}?\s*[,;/ ]\s*{NUM}\s*°?\s*{HEMI_LON}?", re.I),
]
DMS = re.compile(r"(\d{1,3})\s*°\s*(\d{1,2})\s*['′’]\s*(\d{1,2}(?:[.,]\d+)?)\s*(?:\"|″|”|'')?\s*([NSEWСЮВЗМЕ])",
                 re.I)
# OCR с русским словарём путает латинские буквы сторон света с похожими кириллическими
HEMI_FIX = str.maketrans({"М": "N", "м": "N", "Е": "E", "е": "E"})


def _f(x: str) -> float:
    return float(x.replace(",", "."))


def _valid(lat: float, lon: float) -> bool:
    return -90 <= lat <= 90 and -180 <= lon <= 180 and not (abs(lat) < 0.01 and abs(lon) < 0.01)


def _sign(v: float, hemi: str | None, neg: str) -> float:
    return -abs(v) if hemi and hemi.upper() in neg else v


def parse_text(text: str) -> tuple[float, float] | None:
    """Найти координаты в распознанном тексте. Возвращает (lat, lon)."""
    t = text.replace(" ", " ")
    dms = DMS.findall(t)
    if len(dms) >= 2:
        vals = {}
        for d, m, s, h in dms:
            h = h.translate(HEMI_FIX)
            v = int(d) + int(m) / 60 + _f(s) / 3600
            key = "lat" if h.upper() in "NSСЮ" else "lon"
            vals.setdefault(key, _sign(v, h, "SWЮЗ"))
        if len(vals) == 2 and _valid(vals["lat"], vals["lon"]):
            return vals["lat"], vals["lon"]
    for pat in PATTERNS:
        for m in pat.finditer(t):
            lat, hl, lon, hn = _f(m[1]), m[2], _f(m[3]), m[4]
            lat, lon = _sign(lat, hl, "SЮ"), _sign(lon, hn, "WЗ")
            if _valid(lat, lon):
                return lat, lon
    return None


def _rational(v) -> float:
    try:
        return float(v)
    except (TypeError, ValueError, ZeroDivisionError):
        return float(v[0]) / float(v[1])


def from_exif(img: Image.Image) -> dict | None:
    exif = img.getexif()
    gps = exif.get_ifd(GPS_IFD)
    out: dict = {}
    if gps and 2 in gps and 4 in gps:
        try:
            lat = sum(_rational(x) / 60 ** i for i, x in enumerate(gps[2]))
            lon = sum(_rational(x) / 60 ** i for i, x in enumerate(gps[4]))
        except (TypeError, ValueError, ZeroDivisionError):
            lat = lon = None
        if lat is not None:
            lat = _sign(lat, str(gps.get(1, "N")), "S")
            lon = _sign(lon, str(gps.get(3, "E")), "W")
            if _valid(lat, lon):
                out.update(lat=lat, lon=lon, source="exif")
    dt = exif.get_ifd(EXIF_IFD).get(DATETIME_ORIGINAL) or exif.get(306)
    if dt:
        # В EXIF время местное, без пояса — считаем, что снято в часовом поясе системы
        with contextlib.suppress(ValueError):
            out["taken_at"] = datetime.strptime(str(dt).strip("\x00 "), "%Y:%m:%d %H:%M:%S").replace(
                tzinfo=ZoneInfo(get_settings().display_tz))
    return out or None


def from_ocr(img: Image.Image) -> dict | None:
    """Распознать подпись с координатами. Подпись обычно внизу кадра — сначала нижняя треть."""
    try:
        import pytesseract
        pytesseract.get_tesseract_version()
    except Exception:  # noqa: BLE001  — нет Tesseract: просто без OCR
        return None
    img = ImageOps.exif_transpose(img).convert("L")
    if img.width > 2000:
        img = img.resize((2000, round(img.height * 2000 / img.width)))
    w, h = img.size
    # Сначала только латиница (надёжнее для цифр и N/E), затем с русским — для «Широта/Долгота»
    for lang in ("eng", "eng+" + get_settings().tesseract_lang):
        for crop in (img.crop((0, int(h * 0.6), w, h)), img.crop((0, 0, w, int(h * 0.3))), img):
            for variant in (crop, ImageOps.invert(crop)):   # светлый текст на тёмной плашке и наоборот
                try:
                    text = pytesseract.image_to_string(variant, lang=lang, config="--psm 6")
                except Exception:  # noqa: BLE001
                    return None
                found = parse_text(text)
                if found:
                    return {"lat": found[0], "lon": found[1], "source": "ocr", "text": " ".join(text.split())[:200]}
    return None


def extract(data: bytes, filename: str, *, ocr: bool = True) -> dict | None:
    """Координаты съёмки {lat, lon, source: exif|ocr, taken_at?} или None."""
    ext = filename[filename.rfind("."):].lower() if "." in filename else ""
    if ext not in IMAGE_EXT:
        return None
    try:
        img = Image.open(io.BytesIO(data))
        img.load()
    except (UnidentifiedImageError, OSError):
        return None
    meta = from_exif(img) or {}
    if "lat" not in meta and ocr:
        found = from_ocr(img)
        if found:
            meta.update(found)
    return meta or None
