"""Разбор названий объектов из схем: коды колодцев и узлов, концы линий.

Примеры реальных названий (Google Earth, выгрузки инженеров):
  точки:  «Колодец Таттелеком №118», «Колодец ТВТ 02-17», «Колодец 31-16», «Муфта столб "Электротранспорт"»
  линии:  «к.№348 - к.№498», «к.461 - к.482», «К.02-17 - К.53-01», «к.№301-  к.№348», «55135 -57100»,
          «к.31-16 - к.№508 - 57177» (три точки), «Мира 15Б - 53185», «К.№442 - ТатАИСнефть»,
          «КамАЗ», «Untitled Path»

Правила:
- разделитель концов — дефис, у которого хотя бы с одной стороны пробел; дефис внутри кода
  («651-А», «02-17», «Форт-Диалог») разделителем не считается;
- колодец: «к.», «К.», «колодец», затем необязательное слово (владелец), «№», номер вида
  348 / 02-17 / 651-А; в базе хранится в едином виде «к.№348»;
- пятизначное число — код узла/площадки (57121);
- всё остальное — текстовое имя точки (адрес, организация, «ГНБ 14 комплекс»).
"""
import re
from dataclasses import dataclass

SEPARATOR = re.compile(r"\s+-\s*|\s*-\s+")
# Номер колодца: цифры, затем необязательно «-» и цифры/буквы (02-17, 651-А, 31-16)
WELL_NUMBER = r"\d{1,4}(?:-[0-9A-Za-zА-Яа-яЁё]{1,3})?"
WELL = re.compile(
    rf"^(?:колодец|кол\.|к\.?)\s*(?:(?P<owner>[A-Za-zА-Яа-яЁё]{{2,}})\s+)?№?\s*(?P<num>{WELL_NUMBER})$",
    re.IGNORECASE,
)
SITE = re.compile(r"^(?P<code>\d{5,6})$")
CLOSURE = re.compile(r"^(?:муфта|мфт\.?)\s*(?P<rest>.*)$", re.IGNORECASE)
GENERIC_NAMES = {"untitled path", "untitled placemark", "без названия", "новая метка", "путь", "линия"}


@dataclass(frozen=True)
class Endpoint:
    """Разобранная точка из названия."""

    kind: str            # well | site | name
    code: str | None     # канонический код («к.№348», «57121») — по нему привязка
    label: str           # исходный текст
    owner_hint: str | None = None  # слово-владелец из «Колодец Таттелеком №118»


def normalize_well_number(num: str) -> str:
    """Буква в номере — заглавная: «651-а» → «651-А»."""
    return num.upper()


def parse_point_name(text: str | None) -> Endpoint | None:
    if not text:
        return None
    t = " ".join(text.split()).strip(" .")
    if not t or t.lower() in GENERIC_NAMES:
        return None
    m = WELL.match(t)
    if m:
        return Endpoint("well", f"к.№{normalize_well_number(m['num'])}", t, m["owner"])
    m = SITE.match(t)
    if m:
        return Endpoint("site", m["code"], t)
    return Endpoint("name", None, t)


def split_line_name(text: str | None) -> list[str]:
    """«к.№348 - к.№498» → ['к.№348', 'к.№498']; «КамАЗ» → ['КамАЗ']; пустое → []."""
    if not text:
        return []
    t = " ".join(text.split())
    if t.lower() in GENERIC_NAMES:
        return []
    return [p.strip() for p in SEPARATOR.split(t) if p.strip()]


def parse_line_name(text: str | None) -> list[Endpoint]:
    """Концы линии по её названию (2 и более — линия проходит через промежуточные точки)."""
    parts = split_line_name(text)
    if len(parts) < 2:
        return []  # одиночное имя («КамАЗ», «ГНБ 14 комплекс») — концы определяются только по геометрии
    return [e for e in (parse_point_name(p) for p in parts) if e is not None]


def is_generic(text: str | None) -> bool:
    return not text or " ".join(text.split()).lower() in GENERIC_NAMES


def closure_label(text: str | None) -> str | None:
    """«Муфта  столб "Электротранспорт"» → «столб "Электротранспорт"»."""
    if not text:
        return None
    m = CLOSURE.match(" ".join(text.split()))
    return (m["rest"].strip() or None) if m else None
