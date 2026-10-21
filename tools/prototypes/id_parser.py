#!/usr/bin/env python3
"""
Прототип парсера пакета исполнительной документации (ИД) на ВОЛС.

Что делает:
  1. Читает многостраничный PDF, определяет тип каждой страницы по заголовку.
  2. Из страниц с текстовым слоем извлекает данные (опоры, длины, разварку,
     измерения, коммутатор). Сканы (свидетельства о поверке) распознаёт OCR.
  3. Проверяет согласованность данных внутри пакета.
  4. Сохраняет result.json, report.md и poles.geojson.

Запуск:
  python id_parser.py ИД.pdf --out out/ [--ocr]

Зависимости: pdfplumber; для --ocr: pypdfium2, pytesseract, tesseract + rus.
"""
import argparse
import json
import math
import os
import re
from collections import Counter, defaultdict
from datetime import date

import pdfplumber

# ---------------------------------------------------------------------------
# Вспомогательные функции
# ---------------------------------------------------------------------------

MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6,
          "июля": 7, "августа": 8, "сентября": 9, "октября": 10, "ноября": 11,
          "декабря": 12}

# Стандартная 8-цветная маркировка волокон (по порядку номеров).
FIBER_COLORS = ["синий", "оранжевый", "зеленый", "коричневый",
                "серый", "белый", "красный", "черный"]


def num(s):
    """'1 234,5' -> 1234.5; None, если не число."""
    if s is None:
        return None
    s = str(s).replace(" ", "").replace(" ", "").replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def flat(text):
    """Текст в одну строку: убирает переносы, склеивает слова, разорванные дефисом."""
    text = re.sub(r"(\w)-\n(\w)", r"\1\2", text)
    return re.sub(r"\s+", " ", text).strip()


def parse_date(s):
    """'24.07.2026' или '23 июня 2026' -> date."""
    if not s:
        return None
    # OCR путает кириллические «З»/«О» с цифрами 3/0 рядом с цифрами: «З1 июля» -> «31 июля»
    s = re.sub(r"(?<=\d)[Зз]|[Зз](?=\d)", "3", s)
    s = re.sub(r"(?<=\d)[Оо]|[Оо](?=\d)", "0", s)
    m = re.search(r"(\d{1,2})\.(\d{1,2})\.(\d{4})", s)
    if m:
        return date(int(m[3]), int(m[2]), int(m[1]))
    m = re.search(r"(\d{1,2})\s+([а-яё]+)\s+(\d{4})", s.lower())
    if m and m[2] in MONTHS:
        return date(int(m[3]), MONTHS[m[2]], int(m[1]))
    return None


def haversine(lat1, lon1, lat2, lon2):
    """Расстояние между точками в метрах."""
    r = 6371008.8
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = p2 - p1, math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def norm_color(c):
    """'оранжев' -> 'оранжевый' (по префиксу)."""
    if not c:
        return None
    c = c.strip().lower().replace("ё", "е")
    for full in FIBER_COLORS:
        if full.startswith(c) or c.startswith(full[:5]):
            return full
    return c


def data_rows(table, min_filled=2):
    """Строки таблицы, где заполнено хотя бы min_filled ячеек."""
    return [r for r in table if sum(1 for c in r if c not in (None, "")) >= min_filled]


# ---------------------------------------------------------------------------
# 1. Классификация страниц
# ---------------------------------------------------------------------------

# Порядок важен: срабатывает первое совпадение.
PAGE_RULES = [
    ("title",            r"Исполнительная\s+Документация"),
    ("act_pnr",          r"АКТ\s+при[её]мки\s+пуско-наладочных"),
    ("act_pnr_logical",  r"Логические параметры"),
    ("act_acceptance",   r"освидетельствования\s+и\s+приемки"),
    ("line_passport",    r"ПАСПОРТ\s+на\s+законченное\s+строительством"),
    ("act_readiness",    r"АКТ\s+технической\s+готовности"),
    ("conclusion",       r"^\s*Заключение"),
    ("skeleton",         r"Скелетная схема"),
    ("route_scheme",     r"Схема прокладки волоконно-оптического"),
    ("power_scheme",     r"Схема прокладки кабеля питания|Схема подключения электропитания"),
    ("splice_scheme",    r"Схема разварки"),
    ("grounding",        r"Схема заземления"),
    ("rack_plan",        r"План размещения оконечного оборудования"),
    ("closure_protocol", r"Протокол монтажа оптической муфты"),
    ("cross_protocol",   r"Протокол монтажа оптического кросса"),
    ("otdr",             r"рефлектометрические измерения"),
    ("power_meter",      r"измеритель мощности"),
    ("cable_report",     r"Отчет по прокладке кабеля"),
    ("insulation",       r"сопротивления изоляции"),
    ("poles",            r"Ведомость существующих опор"),
    ("changes",          r"Перечень внесенных изменений"),
    # Типы, которые обычно приходят сканами (распознаются OCR)
    ("calibration",      r"СВИДЕТЕЛЬСТВО\s+[ОO0]\s+ПОВЕРКЕ"),
    ("factory_passport", r"Кабель марки"),
    ("declaration",      r"ДЕКЛАРАЦИ"),
    ("tech_passport",    r"ТЕХНИЧЕСКИЙ ПАСПОРТ"),
    ("certificate",      r"СЕРТИФИКАТ"),
    ("cabinet_passport", r"Шкаф\s+настенный"),
]

PAGE_TITLES = {
    "title": "Титульный лист", "act_pnr": "Акт ПНР", "act_pnr_logical": "Акт ПНР (логические параметры)",
    "act_acceptance": "Акт приёмки СМР", "line_passport": "Паспорт линейного сооружения",
    "conclusion": "Заключение комиссии", "act_readiness": "Акт технической готовности", "skeleton": "Скелетная схема",
    "route_scheme": "Схема прокладки ВОК", "power_scheme": "Схема электропитания",
    "splice_scheme": "Схема разварки ОВ", "grounding": "Схема заземления",
    "rack_plan": "План размещения оборудования", "closure_protocol": "Протокол монтажа муфты",
    "cross_protocol": "Протокол монтажа кросса", "otdr": "Протокол рефлектометрии",
    "power_meter": "Протокол измерения тестером", "cable_report": "Отчёт по прокладке кабеля",
    "insulation": "Протокол сопротивления изоляции", "poles": "Ведомость опор и муфт",
    "changes": "Перечень изменений", "calibration": "Свидетельство о поверке",
    "factory_passport": "Заводской паспорт кабеля", "declaration": "Декларация о соответствии",
    "tech_passport": "Технический паспорт изделия", "certificate": "Сертификат",
    "cabinet_passport": "Паспорт шкафа", "unknown": "Не распознано", "empty": "Скан без текста",
}

# Страницы, которые не несут данных о сети — только архив.
ARCHIVE_ONLY = {"declaration", "tech_passport", "certificate", "cabinet_passport",
                "conclusion", "grounding", "power_scheme", "rack_plan"}


def classify(text):
    for kind, pattern in PAGE_RULES:
        if re.search(pattern, text, re.IGNORECASE | re.MULTILINE):
            return kind
    return "unknown"


# ---------------------------------------------------------------------------
# 2. Общие поля страницы (шапка)
# ---------------------------------------------------------------------------

PERSON_RE = re.compile(r"\b([А-ЯЁ][а-яё]+)\s+([А-ЯЁ])\.\s?([А-ЯЁ])\.")


def header_fields(text):
    f = flat(text)
    out = {}
    m = re.search(r"\bСЛ\s*(\d{6,}/\d+)", f)
    if m:
        out["volp"] = m[1]
    m = re.search(r"по адресу\s+(.+?)\s*\\\s*(\d{6,})", f)
    if m:
        out["address"] = m[1].strip()
        out["client_code"] = m[2]
    out["people"] = sorted({f"{a} {b}.{c}." for a, b, c in PERSON_RE.findall(f)})
    out["closures"] = sorted(set(re.findall(r"Муфт[аыеу]\s+(М\d+)", f)))
    return out


# ---------------------------------------------------------------------------
# 3. Извлечение данных по типам страниц
# ---------------------------------------------------------------------------

def parse_endpoint(cell):
    """'16ABCD-ACCF00-001\\n(10.0.0.1) ge0/0/25' -> dict."""
    if not cell:
        return {}
    c = cell.replace("\n", " ")
    out = {}
    m = re.search(r"\b(\d{2}[A-Z]{3,}[A-Z0-9-]+)", c)
    if m:
        out["hostname"] = m[1]
    m = re.search(r"\((\d{1,3}(?:\.\d{1,3}){3})\)", c)
    if m:
        out["ip"] = m[1]
    m = re.search(r"\)\s*(\S+)\s*$", c)
    if m:
        out["port"] = m[1]
    return out


def ex_act_pnr(page, text):
    f = flat(text)
    d = {}
    for key, pat in [("model", r"Марка оборудования:\s*(\S+)"),
                     ("serial", r"Серийный номер:\s*(.+?)(?:\s+Финансовые|$)"),
                     ("address", r"на адрес/\s*(.+?)\s*площадки"),
                     ("date", r"(\d{1,2}\s+[а-я]+\s+\d{4})\s*г\.")]:
        m = re.search(pat, f)
        if m:
            d[key] = m[1].strip()
    for t in page.extract_tables():
        for row in t:
            first = (row[0] or "").strip()
            if first.startswith("Устанавливаемое") or first.startswith("Вышестоящее"):
                key = "device" if first.startswith("Устанавливаемое") else "uplink"
                ep = parse_endpoint(row[1])
                ep.update({"cross": row[2], "cassette": row[3] or None, "cross_port": row[4]})
                d[key] = ep
    return d


def ex_act_pnr_logical(page, text):
    f = flat(text)
    d = {}
    for key, pat in [("hostname", r"HostName:\s*(\S+)"), ("ip", r"IP-адрес:\s*(\S+)"),
                     ("subnet", r"Подсеть IP-адреса:\s*(\S+)"),
                     ("segment", r"Локация/сегмент СПД:\s*(\S+)")]:
        m = re.search(pat, f)
        if m:
            d[key] = m[1]
    return d


def ex_line_passport(page, text):
    f = flat(text)
    d = {}
    m = re.search(r"Кабель\s+(.+?)\s+[–-]\s*([\d,]+)\s*км", f)
    if m:
        d["cable"] = m[1].strip()
        d["total_m"] = round(num(m[2]) * 1000, 1)
    d["parts"] = {k.strip(): num(v) for k, v in re.findall(r"-\s*([^:-]+?):\s*([\d,]+)\s*м", f)}
    m = re.search(r"Паспорт составлен:\s*(\S+)", f)
    if m:
        d["date"] = m[1].rstrip("г.")
    return d


def ex_skeleton(page, text):
    d = {}
    m = re.search(r"Физ\.\s*длина\s*[–-]\s*([\d,]+)", text)
    d["physical_m"] = num(m[1]) if m else None
    m = re.search(r"Опт\.\s*длина\s*[–-]\s*([\d,]+)", text)
    d["optical_m"] = num(m[1]) if m else None
    # Участки: «по фасаду здания», «по сущ.опорам», «по зданию» и их длины по порядку
    # подписи участков разорваны по строкам («ОКГ-8 по» / «фасаду здания»), ищем в тексте одной строкой
    kinds = re.findall(r"(фасаду здания|сущ\.\s*опорам|(?<!сущ\.)опорам|зданию)", flat(text))
    lens = [num(x) for x in re.findall(r"–\s*([\d,]+)м", text)]
    lens = [x for x in lens if x not in (d["physical_m"], d["optical_m"])]
    d["segments"] = [{"type": k, "length_m": l} for k, l in zip(kinds, lens)]
    d["reserves_m"] = [num(x) for x in re.findall(r"^-\s*([\d,]+)\s*м", text, re.MULTILINE)]
    m = re.search(r"\((\d{2}[A-Z]{3,}[A-Z0-9-]+)\)", text)
    if m:
        d["node"] = m[1]
    return d


def ex_route_scheme(page, text):
    f = flat(text)
    d = {"span_labels_m": [num(x) for x in re.findall(r"-\s*(\d+,\d)\s*-", text)]}
    m = re.search(r"(\d+)м\s+ПАО", text)
    d["cable_label_m"] = num(m[1]) if m else None
    vol = {}
    for label, key in [(r"по\s+опорам", "aerial"), (r"по\s+зданию", "building"),
                       (r"Запас ВОК", "reserve"), (r"Итого ВОК", "total")]:
        m = re.search(label + r".{0,60}?м\s+([\d,]+)", f)
        if m:
            vol[key] = num(m[1])
    d["volume"] = vol
    return d


def ex_splice_scheme(page, text):
    d = {}
    # Марки кабелей на схеме написаны повёрнутыми: такой текст читается «задом наперёд»,
    # поэтому ищем марки в развёрнутом тексте страницы.
    rev = text[::-1].replace("\n", "")
    d["cables"] = re.findall(r"((?:ДПО|ДПС|ДПТ|ОКЛ|ОКГ|ОКБ)-[\w().,/ -]+?);", rev)
    joined = re.sub(r"-\n", "-", text)
    d["access_nodes"] = sorted(set(re.findall(r"УД\s*(\d{2}[A-Z]{3,}-[A-Z0-9]+-\d+)", joined)))
    d["closures"] = sorted(set(re.findall(r"Муфта\s+(М\d+)", text)))
    pairs = []
    for t in page.extract_tables():
        if t and t[0] and t[0][0] and "Номер" in t[0][0]:
            for row in t[1:]:
                if row[0] and row[1] and row[2]:
                    colors = row[1].split()
                    pairs.append({"fiber_a": int(row[0]), "color_a": norm_color(colors[0]),
                                  "color_b": norm_color(colors[-1]), "fiber_b": int(row[2])})
    d["splices"] = pairs
    return d


def instruments(text):
    out = []
    f = flat(text)
    m = re.search(r"Сварочное устройство \(тип, зав\. №\):\s*(\S+\s+\S+)\s*#?(\S+)", f)
    if m:
        out.append({"kind": "splicer", "model": m[1], "serial": m[2]})
    m = re.search(r"Рефлектометр \(тип, зав\. №\):\s*(.+?)(?:,\s*заводской номер\s*(\d+))", f)
    if m:
        reg = re.search(r"№\s*(\d{4,5}-\d{2})", m[1])
        out.append({"kind": "otdr", "model": m[1].split(";")[0].strip(),
                    "registry_no": reg[1] if reg else None, "serial": m[2]})
    m = re.search(r"тестер \(тип, зав\. №\):\s*(\S+)\s*№\s*(\S+)", f)
    if m:
        out.append({"kind": "power_meter", "model": m[1], "registry_no": m[2], "serial": None})
    return out


def ex_splice_protocol(page, text):
    """Протокол монтажа муфты / кросса в узле: таблица сварок."""
    f = flat(text)
    d = {"instruments": instruments(text), "splices": []}
    m = re.search(r"Место установки:\s*(.+?)\s+Сварочное", f)
    if m:
        d["place"] = m[1]
    for t in page.extract_tables():
        if len(t) < 4 or len(t[0]) < 10:
            continue
        cable_a, cable_b = (t[0][0] or "").strip(" ;"), (t[0][7] or "").strip(" ;")
        for row in data_rows(t[3:], 3):
            d["splices"].append({
                "cable_a": cable_a, "module_a": row[0] or None, "fiber_a": int(row[1]),
                "color_a": norm_color(row[2]),
                "loss_otdr_ab": num(row[3]), "loss_otdr_ba": num(row[4]),
                "loss_otdr_avg": num(row[5]), "loss_splicer": num(row[6]),
                "cable_b": cable_b, "color_b": norm_color(row[7]),
                "fiber_b": int(row[8]), "module_b": row[9] or None})
    m = re.search(r"(\d+)\s*м\s+Запас ОК", text)
    if m:
        d["reserve_m"] = num(m[1])
    return d


def ex_cross_termination(page, text):
    """Протокол монтажа оконечного кросса: волокна на порты."""
    f = flat(text)
    d = {"instruments": instruments(text), "ports": []}
    for key, pat in [("cross", r"Наименование \(номер\) кросса:\s*(\S+)"),
                     ("address", r"Адрес кросса:\s*(.+?)\s+Уточнение"),
                     ("connector", r"Тип разъема на кроссе:\s*(\S+)")]:
        m = re.search(pat, f)
        if m:
            d[key] = m[1]
    for t in page.extract_tables():
        for row in t:
            if row and row[0] and str(row[0]).strip().isdigit() and len(row) >= 4:
                d["ports"].append({"fiber": int(row[0]), "color": norm_color(row[1]),
                                   "spliced": row[3] not in (None, ""), "loss": num(row[3])})
    return d


def ex_measurement(page, text, kind):
    f = flat(text)
    d = {"kind": kind, "instruments": instruments(text)}
    for key, pat in [("wavelength_nm", r"длина волны:\s*(\d+)"),
                     ("refractive_index", r"показатель преломления:\s*([\d,]+)"),
                     ("cable", r"тип кабеля:\s*(.+?);"),
                     ("permitted_db_km", r"допустимое затухание:\s*([\d,]+)"),
                     ("direction_ab", r"Направление А-Б:\s*(.+?)\s+Направление"),
                     ("date", r"(\d{2}\.\d{2}\.\d{4})")]:
        m = re.search(pat, f)
        if m:
            d[key] = m[1].strip()
    for k in ("refractive_index", "permitted_db_km", "wavelength_nm"):
        if k in d:
            d[k] = num(d[k])
    d["rows"] = []
    for t in page.extract_tables():
        for row in data_rows(t[2:], 3):
            d["rows"].append({"port_a": row[0], "loss_ab": num(row[1]), "loss_ba": num(row[2]),
                              "km_ab": num(row[3]), "km_ba": num(row[4]),
                              "port_b": row[5], "optical_km": num(row[6])})
    return d


def ex_cable_report(page, text):
    f = flat(text)
    d = {"rows": []}
    m = re.search(r"\d\)\.\s*От\s+(.+?)\s+до\s+(.+?)\.\s", f)
    if m:
        d["from"], d["to"] = m[1], m[2]
    m = re.search(r"Завод изготовитель кабеля:\s*(\S+)", f)
    if m:
        d["manufacturer"] = m[1]
    for t in page.extract_tables():
        for row in t[1:]:
            if row[0] and row[0] != "Итого":
                d["rows"].append({"drum": row[0], "between": flat(row[2] or ""),
                                  "received_m": num(row[3]), "laid_m": num(row[4]),
                                  "remainder_m": num(row[5]), "year": row[6]})
    return d


def ex_insulation(page, text):
    d = {"rows": []}
    for t in page.extract_tables():
        for row in t:
            if row and row[0] and str(row[0]).strip().isdigit() and row[2]:
                d["rows"].append({"section": flat(row[1] or ""), "length_km": num(row[2]),
                                  "r_mohm": num(row[3]), "r_km_mohm_km": num(row[4])})
    return d


def ex_poles(page, text):
    d = {"poles": []}
    for t in page.extract_tables():
        for row in t:
            if row and row[3] and re.search(r"\d+\.\d+,\s*\d+\.\d+", row[3]):
                lat, lon = [float(x) for x in row[3].split(",")]
                d["poles"].append({"n": int(row[0]), "type": flat(row[1] or ""),
                                   "name": row[2], "lat": lat, "lon": lon,
                                   "note": row[4] or None})
    return d


EXTRACTORS = {
    "act_pnr": ex_act_pnr, "act_pnr_logical": ex_act_pnr_logical,
    "line_passport": ex_line_passport, "skeleton": ex_skeleton,
    "route_scheme": ex_route_scheme, "splice_scheme": ex_splice_scheme,
    "closure_protocol": ex_splice_protocol,
    "otdr": lambda p, t: ex_measurement(p, t, "otdr"),
    "power_meter": lambda p, t: ex_measurement(p, t, "power_meter"),
    "cable_report": ex_cable_report, "insulation": ex_insulation, "poles": ex_poles,
}


def ex_cross(page, text):
    # Два вида «протокола монтажа кросса»: сварки в узле или оконечный кросс с портами.
    return ex_cross_termination(page, text) if "Тип разъема" in text else ex_splice_protocol(page, text)


EXTRACTORS["cross_protocol"] = ex_cross


# ---------------------------------------------------------------------------
# 4. OCR сканов (свидетельства о поверке)
# ---------------------------------------------------------------------------

def ocr_pages(pdf_path, page_numbers):
    """Возвращает {номер страницы: (текст, срок действия из строки «Действительно до»)}."""
    import pypdfium2 as pdfium
    import pytesseract
    doc = pdfium.PdfDocument(pdf_path)
    out = {}
    for n in page_numbers:
        img = doc[n - 1].render(scale=3).to_pil().convert("L")
        text = pytesseract.image_to_string(img, lang="rus")
        valid = None
        if re.search(r"ПОВЕРКЕ", text):
            valid = ocr_valid_until(img, text, pytesseract)
        out[n] = (text, valid)
    return out


def ocr_valid_until(img, text, pytesseract):
    """Срок действия поверки. Если дата не в той же строке, вырезаем полосу справа от слов."""
    m = re.search(r"[Дд]ействительн\w*\s+до\s*(\d{2}\.\d{2}\.\d{4}|\d{1,2}\s+[а-я]+\s+\d{4})", text)
    if m:
        return m[1]
    data = pytesseract.image_to_data(img, lang="rus", output_type=pytesseract.Output.DICT)
    for i, w in enumerate(data["text"]):
        if w.lower().startswith("действительн"):
            top, h = data["top"][i], data["height"][i]
            x0 = data["left"][i] + data["width"][i]
            crop = img.crop((x0, max(0, top - h), img.width, top + 2 * h))
            line = pytesseract.image_to_string(crop, lang="rus", config="--psm 7")
            d = parse_date(line)
            if d:
                return d.strftime("%d.%m.%Y")
    return None


def ex_calibration(text, valid):
    f = flat(text)
    d = {"valid_until": valid}
    for key, pat in [("number", r"ПОВЕРКЕ\s*№\s*(\S+)"),
                     ("instrument", r"Средство измерений\s+(.+?)\s+наименование"),
                     ("serial", r"заводской номер\s+(\S+)")]:
        m = re.search(pat, f)
        if m:
            d[key] = m[1].strip(" ,")
    reg = re.search(r"№\s*(\d{4,5}-\d{2})", d.get("instrument", ""))
    d["registry_no"] = reg[1] if reg else None
    return d


# ---------------------------------------------------------------------------
# 5. Проверки согласованности
# ---------------------------------------------------------------------------

class Findings:
    def __init__(self):
        self.items = []

    def add(self, level, code, msg, pages=None):
        self.items.append({"level": level, "code": code, "message": msg,
                           "pages": sorted(set(pages or []))})


def majority_check(fnd, pages, field, code, title):
    vals = defaultdict(list)
    for p in pages:
        v = p["header"].get(field)
        if v:
            vals[v].append(p["page"])
    if len(vals) > 1:
        main = max(vals, key=lambda k: len(vals[k]))
        for v, pg in vals.items():
            if v != main:
                fnd.add("error", code, f"{title}: «{v}» вместо «{main}» (основное значение пакета)", pg)
    return max(vals, key=lambda k: len(vals[k])) if vals else None


def run_checks(pages, ex, ocr):
    fnd = Findings()
    by_kind = defaultdict(list)
    for p in pages:
        by_kind[p["kind"]].append(p)

    # --- шапки страниц
    volp = majority_check(fnd, pages, "volp", "VOLP_MISMATCH", "Номер ВОЛП")
    majority_check(fnd, pages, "client_code", "CLIENT_MISMATCH", "Код объекта/клиента")

    closures = defaultdict(list)
    for p in pages:
        for c in p["header"]["closures"]:
            closures[c].append(p["page"])
    if len(closures) > 1:
        main = max(closures, key=lambda k: len(closures[k]))
        for c, pg in closures.items():
            if c != main:
                fnd.add("warning", "CLOSURE_NAME", f"Муфта «{c}» упоминается только здесь; "
                        f"на остальных страницах — «{main}»", pg)

    people = defaultdict(lambda: defaultdict(list))
    for p in pages:
        for person in p["header"]["people"]:
            surname, initials = person.split(" ", 1)
            people[surname][initials].append(p["page"])
    for surname, variants in people.items():
        if len(variants) > 1:
            main = max(variants, key=lambda k: len(variants[k]))
            for ini, pg in variants.items():
                if ini != main:
                    fnd.add("warning", "PERSON_INITIALS",
                            f"{surname} {ini} — на остальных страницах {surname} {main}", pg)

    # --- длины кабеля из разных документов
    lengths = []
    if "line_passport" in ex:
        lengths.append(("Паспорт линейного сооружения", ex["line_passport"]["data"].get("total_m"),
                        ex["line_passport"]["page"]))
    if "skeleton" in ex:
        lengths.append(("Скелетная схема (физ. длина)", ex["skeleton"]["data"].get("physical_m"),
                        ex["skeleton"]["page"]))
    if "route_scheme" in ex:
        rs = ex["route_scheme"]["data"]
        lengths.append(("Схема прокладки (объёмная ведомость)", rs["volume"].get("total"),
                        ex["route_scheme"]["page"]))
        lengths.append(("Схема прокладки (подпись у кабеля)", rs.get("cable_label_m"),
                        ex["route_scheme"]["page"]))
    if "cable_report" in ex:
        for r in ex["cable_report"]["data"]["rows"]:
            lengths.append(("Отчёт по прокладке (проложено)", r["laid_m"], ex["cable_report"]["page"]))
    if "insulation" in ex:
        for r in ex["insulation"]["data"]["rows"]:
            lengths.append(("Протокол изоляции (физ. длина)", r["length_km"] * 1000,
                            ex["insulation"]["page"]))
    vals = [v for _, v, _ in lengths if v]
    if vals:
        main = Counter(vals).most_common(1)[0][0]
        for name, v, pg in lengths:
            if v and abs(v - main) > 0.5:
                fnd.add("error", "LENGTH_MISMATCH",
                        f"{name}: {v:g} м, в остальных документах {main:g} м", [pg])

    # --- паспорт: сумма составляющих
    if "line_passport" in ex:
        lp = ex["line_passport"]["data"]
        s = sum(v for v in lp["parts"].values() if v)
        if lp.get("total_m") and abs(s - lp["total_m"]) > 0.5:
            fnd.add("error", "PASSPORT_SUM", f"Сумма составляющих {s:g} м ≠ итог {lp['total_m']:g} м",
                    [ex["line_passport"]["page"]])

    # --- скелетная схема: участки + запасы = физ. длина; физ. vs опт.
    if "skeleton" in ex:
        sk = ex["skeleton"]["data"]
        s = sum(x["length_m"] for x in sk["segments"]) + sum(sk["reserves_m"])
        if sk.get("physical_m") and abs(s - sk["physical_m"]) > 0.5:
            fnd.add("error", "SKELETON_SUM", f"Участки + запасы = {s:g} м ≠ физ. длине {sk['physical_m']:g} м",
                    [ex["skeleton"]["page"]])
        if sk.get("physical_m") and sk.get("optical_m"):
            k = (sk["optical_m"] / sk["physical_m"] - 1) * 100
            fnd.add("info", "TWIST_FACTOR", f"Опт./физ. длина = {sk['optical_m']:g}/{sk['physical_m']:g} м → "
                    f"укрутка волокна ≈ {k:.2f} %", [ex["skeleton"]["page"]])

    # --- опоры: пролёты по координатам vs подписи на схеме
    if "poles" in ex:
        poles = ex["poles"]["data"]["poles"]
        spans = [haversine(a["lat"], a["lon"], b["lat"], b["lon"]) for a, b in zip(poles, poles[1:])]
        ex["poles"]["data"]["spans_m"] = [round(x, 1) for x in spans]
        labels = ex.get("route_scheme", {}).get("data", {}).get("span_labels_m", [])
        lab = Counter(labels).most_common(1)[0][0] if labels else None
        pgs = [ex["poles"]["page"]] + ([ex["route_scheme"]["page"]] if lab else [])
        for i, d in enumerate(spans, 1):
            if lab and abs(d - lab) / lab > 0.2:
                fnd.add("warning", "SPAN_LENGTH",
                        f"Пролёт опора №{i}–№{i + 1}: {d:.1f} м по координатам, на схеме {lab:g} м", pgs)
        aerial = ex.get("line_passport", {}).get("data", {}).get("parts", {}).get("по опорам")
        msg = f"Сумма пролётов между опорами №1–№{len(poles)} по координатам: {sum(spans):.1f} м"
        if lab:
            msg += f" (на схеме {len(spans)}×{lab:g} = {len(spans) * lab:g} м)"
        if aerial:
            msg += f"; в паспорте «по опорам» {aerial:g} м (включает спуски к муфте и зданию)"
        fnd.add("info", "SPANS_TOTAL", msg, pgs)

    # --- измерения
    meas = [e for k in ("otdr", "power_meter") for e in by_kind[k]]
    opt = []
    for p in meas:
        d = p["data"]
        for r in d["rows"]:
            opt.append((d["kind"], r["optical_km"], p["page"]))
            perm = d.get("permitted_db_km")
            if d["kind"] == "otdr" and perm and r["km_ab"] and r["km_ab"] > perm:
                fnd.add("warning", "ATTENUATION",
                        f"Километрическое затухание {r['km_ab']:g} дБ/км ({d.get('wavelength_nm'):g} нм) "
                        f"выше допустимого {perm:g} дБ/км, указанного в том же протоколе", [p["page"]])
    if len(opt) > 1:
        a, b = opt[0][1], opt[1][1]
        if a and b:
            lvl = "info" if abs(a - b) * 1000 <= 5 else "warning"
            fnd.add(lvl, "OPTICAL_LENGTH",
                    f"Оптическая длина тракта: рефлектометр {a:g} км, тестер {b:g} км "
                    f"(разница {abs(a - b) * 1000:.0f} м)", [x[2] for x in opt])

    # --- нулевые потери на сварках
    zero_pages = []
    for k in ("closure_protocol", "cross_protocol"):
        for p in by_kind[k]:
            for s in p["data"].get("splices", []):
                if all(s.get(x) == 0 for x in ("loss_otdr_ab", "loss_otdr_ba", "loss_splicer")):
                    zero_pages.append(p["page"])
    if zero_pages:
        fnd.add("info", "ZERO_LOSS", "Затухание на сварках везде 0,00 дБ (и по рефлектометру, и по "
                "аппарату) — похоже на незаполненные значения", zero_pages)

    # --- изоляция: километрическое сопротивление = R × L
    if "insulation" in ex:
        for r in ex["insulation"]["data"]["rows"]:
            if r["r_mohm"] and r["length_km"] and r["r_km_mohm_km"]:
                calc = r["r_mohm"] * r["length_km"]
                if abs(calc - r["r_km_mohm_km"]) / calc > 0.05:
                    fnd.add("warning", "INSULATION_KM",
                            f"Километрическое сопротивление: указано {r['r_km_mohm_km']:g} МОм·км, "
                            f"расчёт {r['r_mohm']:g} МОм × {r['length_km']:g} км = {calc:g} МОм·км",
                            [ex["insulation"]["page"]])

    # --- цвета волокон по стандартной маркировке
    for k in ("closure_protocol", "cross_protocol", "splice_scheme"):
        for p in by_kind[k]:
            for s in p["data"].get("splices", []):
                for side in ("a", "b"):
                    n, c = s.get("fiber_" + side), s.get("color_" + side)
                    if n and c and FIBER_COLORS[(n - 1) % 8] != c and FIBER_COLORS[(n - 1) % 4] != c:
                        fnd.add("warning", "FIBER_COLOR",
                                f"Волокно №{n} отмечено цветом «{c}», по маркировке — "
                                f"«{FIBER_COLORS[(n - 1) % 8]}»", [p["page"]])

    # --- согласованность портов: акт ПНР ↔ рефлектометрия
    pnr = ex.get("act_pnr", {}).get("data", {})
    if pnr and meas:
        r = meas[0]["data"]["rows"][0] if meas[0]["data"]["rows"] else {}
        dev_port = (pnr.get("device") or {}).get("cross_port")
        up_port = (pnr.get("uplink") or {}).get("cross_port")
        ok = str(r.get("port_a")) == str(dev_port) and str(r.get("port_b")) == str(up_port)
        fnd.add("info" if ok else "error", "PORTS",
                f"Порты тракта: акт ПНР {dev_port} → {up_port}, протокол измерений "
                f"{r.get('port_a')} → {r.get('port_b')}" + (" — совпадают" if ok else " — НЕ совпадают"),
                [ex["act_pnr"]["page"], meas[0]["page"]])

    # --- поверка приборов
    pkg_date = parse_date(ex.get("line_passport", {}).get("data", {}).get("date"))
    used = {}
    for p in pages:
        for ins in p["data"].get("instruments", []) if isinstance(p.get("data"), dict) else []:
            if ins["kind"] in ("otdr", "power_meter"):
                key = ins.get("serial") or ins.get("registry_no")
                used.setdefault(key, (ins, set()))[1].add(p["page"])
                d = parse_date(p["data"].get("date")) or pkg_date
                used[key][1].add(("date", d))
    certs = [(n, ex_calibration(t, v)) for n, (t, v) in ocr.items() if re.search(r"ПОВЕРКЕ", t)]
    for key, (ins, info) in used.items():
        pg = [x for x in info if isinstance(x, int)]
        dates = [x[1] for x in info if isinstance(x, tuple) and x[1]]
        meas_date = max(dates) if dates else None
        match = None
        for n, c in certs:
            if ins.get("serial") and c.get("serial") == ins["serial"]:
                match = (n, c, "serial")
            elif not match and ins.get("registry_no") and c.get("registry_no") == ins["registry_no"]:
                match = (n, c, "registry")
        name = f"{ins['model']} (зав. № {ins.get('serial') or '—'})"
        if not certs:
            fnd.add("info", "CALIBRATION", f"{name}: свидетельства о поверке не распознаны "
                    "(запустите с --ocr)", pg)
            continue
        if not match:
            fnd.add("warning", "CALIBRATION", f"{name}: свидетельство о поверке не найдено в пакете", pg)
            continue
        n, c, how = match
        if how == "registry":
            fnd.add("warning", "CALIBRATION_SERIAL",
                    f"{name}: в протоколе указан номер в Госреестре ({ins['registry_no']}), а не заводской; "
                    f"свидетельство найдено по номеру Госреестра, зав. № в нём {c.get('serial')}", pg + [n])
        vu = parse_date(c.get("valid_until"))
        if vu and meas_date and meas_date > vu:
            fnd.add("error", "CALIBRATION_EXPIRED",
                    f"{name}: измерения {meas_date:%d.%m.%Y}, поверка действительна до {vu:%d.%m.%Y} — "
                    "на дату измерений истекла", pg + [n])
        elif vu and meas_date:
            fnd.add("info", "CALIBRATION_OK",
                    f"{name}: поверка до {vu:%d.%m.%Y}, измерения {meas_date:%d.%m.%Y} — в порядке", pg + [n])
        elif not vu:
            fnd.add("warning", "CALIBRATION", f"{name}: не удалось распознать срок поверки", [n])
    return fnd.items


# ---------------------------------------------------------------------------
# 6. Трасса волокна по протоколам
# ---------------------------------------------------------------------------

def build_trace(ex, by_kind):
    """Цепочка: кросс узла → сварки → оконечный кросс."""
    steps = []
    pnr = ex.get("act_pnr", {}).get("data", {})
    up = pnr.get("uplink") or {}
    if up:
        steps.append(f"{up.get('cross')} порт {up.get('cross_port')} ({up.get('hostname')})")
    protos = [p for k in ("cross_protocol", "closure_protocol") for p in by_kind[k]
              if p["data"].get("splices")]
    # Упорядочиваем сварки: следующий протокол начинается с кабеля, которым закончился предыдущий
    chain, rest = [], protos[:]
    while rest:
        nxt = None
        if not chain:
            ends = {p["data"]["splices"][0]["cable_b"] for p in rest}
            nxt = next((p for p in rest if p["data"]["splices"][0]["cable_a"] not in ends), rest[0])
        else:
            prev = chain[-1]["data"]["splices"][0]
            nxt = next((p for p in rest if p["data"]["splices"][0]["cable_a"] == prev["cable_b"]
                        and p["data"]["splices"][0]["fiber_a"] == prev["fiber_b"]), None)
            if not nxt:
                steps.append("⚠ разрыв цепочки: следующая сварка не найдена")
                nxt = rest[0]
        chain.append(nxt)
        rest.remove(nxt)
    for p in chain:
        s = p["data"]["splices"][0]
        steps.append(f"{s['cable_a']} ОВ {s['fiber_a']} ({s['color_a']}) ⨯ сварка в «{p['data'].get('place')}» ⨯ "
                     f"{s['cable_b']} ОВ {s['fiber_b']} ({s['color_b']})")
    for p in by_kind["cross_protocol"]:
        for port in p["data"].get("ports", []):
            if port["spliced"]:
                steps.append(f"{p['data'].get('cross')} ({p['data'].get('address')}) порт {port['fiber']} "
                             f"({port['color']})")
    dev = pnr.get("device") or {}
    if dev:
        steps.append(f"коммутатор {dev.get('hostname')} ({dev.get('ip')}), {pnr.get('model')}")
    return steps


# ---------------------------------------------------------------------------
# 7. Запуск и отчёты
# ---------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser(description="Парсер пакета ИД на ВОЛС")
    ap.add_argument("pdf")
    ap.add_argument("--out", default="out")
    ap.add_argument("--ocr", action="store_true", help="распознавать сканы (нужен tesseract rus)")
    args = ap.parse_args()
    os.makedirs(args.out, exist_ok=True)

    pages = []
    with pdfplumber.open(args.pdf) as pdf:
        for i, page in enumerate(pdf.pages, 1):
            text = page.extract_text() or ""
            meaningful = len(re.findall(r"[А-Яа-яA-Za-z]{3,}", text))
            kind = classify(text) if meaningful >= 5 else "empty"
            data = {}
            if kind in EXTRACTORS:
                try:
                    data = EXTRACTORS[kind](page, text)
                except Exception as e:  # прототип: ошибку извлечения фиксируем, не падаем
                    data = {"error": repr(e)}
            pages.append({"page": i, "kind": kind, "has_text": kind != "empty",
                          "header": header_fields(text), "data": data})

    ocr = {}
    scans = [p["page"] for p in pages if p["kind"] == "empty"]
    if args.ocr and scans:
        ocr = ocr_pages(args.pdf, scans)
        for p in pages:
            if p["page"] in ocr:
                text, valid = ocr[p["page"]]
                p["kind"] = classify(text)
                p["ocr"] = True
                if p["kind"] == "calibration":
                    p["data"] = ex_calibration(text, valid)

    by_kind = defaultdict(list)
    for p in pages:
        by_kind[p["kind"]].append(p)
    ex = {k: v[0] for k, v in by_kind.items()}  # первая страница каждого типа

    findings = run_checks(pages, ex, ocr)
    trace = build_trace(ex, by_kind)

    result = {"source": os.path.basename(args.pdf), "pages": pages,
              "trace": trace, "findings": findings}
    with open(os.path.join(args.out, "result.json"), "w", encoding="utf-8") as fh:
        json.dump(result, fh, ensure_ascii=False, indent=2, default=str)

    write_geojson(ex, os.path.join(args.out, "poles.geojson"))
    write_report(result, ex, os.path.join(args.out, "report.md"))
    print(f"Страниц: {len(pages)}, замечаний: "
          f"{sum(f['level'] == 'error' for f in findings)} ошибок, "
          f"{sum(f['level'] == 'warning' for f in findings)} предупреждений → {args.out}/")


def write_geojson(ex, path):
    poles = ex.get("poles", {}).get("data", {}).get("poles", [])
    feats = [{"type": "Feature", "geometry": {"type": "Point", "coordinates": [p["lon"], p["lat"]]},
              "properties": {"kind": "pole", "name": p["name"], "type": p["type"]}} for p in poles]
    if len(poles) > 1:
        feats.append({"type": "Feature",
                      "geometry": {"type": "LineString", "coordinates": [[p["lon"], p["lat"]] for p in poles]},
                      "properties": {"kind": "route_segment", "type": "aerial"}})
    with open(path, "w", encoding="utf-8") as fh:
        json.dump({"type": "FeatureCollection", "features": feats}, fh, ensure_ascii=False, indent=2)


def write_report(result, ex, path):
    L = [f"# Разбор пакета ИД: {result['source']}", ""]
    hdr = Counter(p["header"].get("volp") for p in result["pages"] if p["header"].get("volp"))
    if hdr:
        L += [f"**ВОЛП:** СЛ {hdr.most_common(1)[0][0]}", ""]

    L += ["## Страницы", "", "| Стр. | Тип | Текст | Использование |", "|---|---|---|---|"]
    for p in result["pages"]:
        use = "данные" if p["kind"] in EXTRACTORS or p["kind"] == "calibration" else "архив"
        src = "OCR" if p.get("ocr") else ("есть" if p["has_text"] else "нет")
        L.append(f"| {p['page']} | {PAGE_TITLES.get(p['kind'], p['kind'])} | {src} | {use} |")

    L += ["", "## Извлечённые объекты", ""]
    pnr = ex.get("act_pnr", {}).get("data", {})
    if pnr:
        dev, up = pnr.get("device", {}), pnr.get("uplink", {})
        L += ["**Узел доступа (из акта ПНР)**", "",
              f"- {pnr.get('model')}, зав. № {pnr.get('serial')}",
              f"- {dev.get('hostname')} / {dev.get('ip')}, порт {dev.get('port')}, кросс {dev.get('cross')} порт {dev.get('cross_port')}",
              f"- uplink: {up.get('hostname')} / {up.get('ip')} {up.get('port')}, кросс {up.get('cross')} ({up.get('cassette')}) порт {up.get('cross_port')}",
              f"- адрес: {pnr.get('address')}", ""]
    poles = ex.get("poles", {}).get("data", {})
    if poles:
        L += ["**Опоры (узлы сети)**", "", "| № | Обозначение | Широта | Долгота | Пролёт до следующей, м |",
              "|---|---|---|---|---|"]
        spans = poles.get("spans_m", []) + [None]
        for p, s in zip(poles["poles"], spans):
            L.append(f"| {p['n']} | {p['name']} | {p['lat']} | {p['lon']} | {s if s is not None else '—'} |")
        L.append("")
    sk = ex.get("skeleton", {}).get("data", {})
    if sk:
        L += ["**Участки кабеля (скелетная схема)**", ""]
        L += [f"- {s['type']}: {s['length_m']:g} м" for s in sk["segments"]]
        L += [f"- тех. запасы: {', '.join(f'{x:g} м' for x in sk['reserves_m'])}",
              f"- физическая длина {sk.get('physical_m'):g} м, оптическая {sk.get('optical_m'):g} м", ""]
    meas = [p for p in result["pages"] if p["kind"] in ("otdr", "power_meter")]
    if meas:
        L += ["**Измерения тракта**", "", "| Прибор | λ, нм | Порт A → B | Затухание, дБ | дБ/км | Опт. длина, км |",
              "|---|---|---|---|---|---|"]
        for p in meas:
            d = p["data"]
            for r in d["rows"]:
                ins = d["instruments"][0]["model"] if d["instruments"] else d["kind"]
                L.append(f"| {ins} | {d.get('wavelength_nm'):g} | {r['port_a']} → {r['port_b']} | "
                         f"{r['loss_ab']:g} / {r['loss_ba']:g} | {r['km_ab']:g} | {r['optical_km']:g} |")
        L.append("")
    if result["trace"]:
        L += ["**Трасса волокна (собрана из протоколов)**", ""]
        L += [f"{i}. {s}" for i, s in enumerate(result["trace"], 1)]
        L.append("")

    L += ["## Проверка согласованности", ""]
    icons = {"error": "🔴 Ошибка", "warning": "🟡 Предупреждение", "info": "🔵 Инфо"}
    for lvl in ("error", "warning", "info"):
        for f in [x for x in result["findings"] if x["level"] == lvl]:
            pg = ", ".join(str(x) for x in f["pages"])
            L.append(f"- **{icons[lvl]}** — {f['message']}" + (f" _(стр. {pg})_" if pg else ""))
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(L) + "\n")


if __name__ == "__main__":
    main()
