"""Правило «один конец волокна — одно подключение» и начальные справочники

1. Триггер: каждый конец волокна (a — у начала кабеля, b — у конца) может участвовать
   не более чем в одной сварке ИЛИ быть заведён не более чем на один порт кросса.
   Проверка в базе гарантирует целостность рассветки при любом способе записи
   (API, импорт, ручной SQL), а не только через код приложения.
2. Справочники: источники длин с погрешностью, владельцы, операторы, схемы цветовой
   маркировки, типовые марки кабеля.

Revision ID: 0003
Revises: 0002
Create Date: 2026-10-21
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003"
down_revision: str | None = "0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


FIBER_END_FUNCTION = r"""
CREATE OR REPLACE FUNCTION fiber_end_usage(p_fiber int, p_end varchar,
                                           p_skip_splice int, p_skip_port int)
RETURNS int LANGUAGE sql STABLE AS $$
    SELECT
      (SELECT count(*) FROM splice
        WHERE id IS DISTINCT FROM p_skip_splice
          AND ((fiber_a_id = p_fiber AND end_a = p_end) OR (fiber_b_id = p_fiber AND end_b = p_end)))
    + (SELECT count(*) FROM cross_port
        WHERE id IS DISTINCT FROM p_skip_port
          AND fiber_id = p_fiber AND fiber_end = p_end)
$$;

CREATE OR REPLACE FUNCTION splice_check_fiber_ends() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.fiber_a_id = NEW.fiber_b_id AND NEW.end_a = NEW.end_b THEN
        RAISE EXCEPTION 'Нельзя сварить конец волокна сам с собой' USING ERRCODE = 'check_violation';
    END IF;
    IF fiber_end_usage(NEW.fiber_a_id, NEW.end_a, NEW.id, NULL) > 0 THEN
        RAISE EXCEPTION 'Конец % волокна id=% уже подключён (сварка или порт кросса)',
            NEW.end_a, NEW.fiber_a_id USING ERRCODE = 'unique_violation';
    END IF;
    IF fiber_end_usage(NEW.fiber_b_id, NEW.end_b, NEW.id, NULL) > 0 THEN
        RAISE EXCEPTION 'Конец % волокна id=% уже подключён (сварка или порт кросса)',
            NEW.end_b, NEW.fiber_b_id USING ERRCODE = 'unique_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE OR REPLACE FUNCTION cross_port_check_fiber_end() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    IF NEW.fiber_id IS NOT NULL
       AND fiber_end_usage(NEW.fiber_id, NEW.fiber_end, NULL, NEW.id) > 0 THEN
        RAISE EXCEPTION 'Конец % волокна id=% уже подключён (сварка или порт кросса)',
            NEW.fiber_end, NEW.fiber_id USING ERRCODE = 'unique_violation';
    END IF;
    RETURN NEW;
END $$;

CREATE TRIGGER trg_splice_fiber_ends BEFORE INSERT OR UPDATE ON splice
    FOR EACH ROW EXECUTE FUNCTION splice_check_fiber_ends();
CREATE TRIGGER trg_cross_port_fiber_end BEFORE INSERT OR UPDATE ON cross_port
    FOR EACH ROW EXECUTE FUNCTION cross_port_check_fiber_end();
"""

# Справочные данные -----------------------------------------------------------------

LENGTH_SOURCES = [
    # code, name, погрешность %, приоритет (меньше — достовернее)
    ("reflectometer", "Измерено рефлектометром", 1.0, 1),
    ("as_built", "Исполнительная документация", 2.0, 2),
    ("passport", "Паспорт трассы", 5.0, 3),
    ("engineer", "Со слов инженера", 10.0, 4),
    ("geometry", "По геометрии на карте", 25.0, 5),
]

OWNERS = [
    ("ПАО «Таттелеком»", "Таттелеком", "#1f77b4"),
    ("Телесет", "Телесет", "#17becf"),
    ("ТВТ", "ТВТ", "#9467bd"),
    ("КамГЭС", "КамГЭС", "#8c564b"),
    ("ПАО «ВымпелКом»", "ВымпелКом", "#bcbd22"),
    ("ПАО «Ростелеком»", "Ростелеком", "#7f3fbf"),
    ("Собственник здания", "Здание", "#7f7f7f"),
]

OPERATORS = [
    ("ПАО «Ростелеком»", "Ростелеком", "#7b2fbe"),
    ("ПАО «ВымпелКом» (Билайн)", "Билайн", "#e0a800"),
    ("ПАО «МегаФон»", "МегаФон", "#00a650"),
    ("ПАО «МТС»", "МТС", "#d7141a"),
]

# Цвета волокон
FIBER_8 = [("синий", "#1f5fd1"), ("оранжевый", "#f08a24"), ("зелёный", "#2e9e44"), ("коричневый", "#7a4a26"),
           ("серый", "#8c8c8c"), ("белый", "#f4f4f4"), ("красный", "#d62728"), ("чёрный", "#111111")]
FIBER_12 = FIBER_8 + [("жёлтый", "#f2d024"), ("фиолетовый", "#8e44ad"), ("розовый", "#f19cbb"),
                      ("бирюзовый", "#1abc9c")]
# Модули: первый — счётный (красный), остальные натуральные
MODULES_RED_NATURAL = [("красный", "#d62728")] + [("натуральный", "#e9e4d4")] * 11

COLOR_SCHEMES = {
    "Типовая РФ (8 цветов, счётный модуль красный)": (FIBER_8, MODULES_RED_NATURAL),
    "TIA-598 (12 цветов)": (FIBER_12, FIBER_12),
}

# mark, модулей, волокон в модуле, схема
CABLE_TYPES = [
    ("ОКГ-8 (G.652.D)-T/П 3,0", 1, 8, "Типовая РФ (8 цветов, счётный модуль красный)"),
    ("ОКЛ-8 (G.652.D)-T", 1, 8, "Типовая РФ (8 цветов, счётный модуль красный)"),
    ("ДПО-П-08У (2х4)-300Н", 2, 4, "Типовая РФ (8 цветов, счётный модуль красный)"),
    ("ДПС-П-16У (2х8)-7кН", 2, 8, "Типовая РФ (8 цветов, счётный модуль красный)"),
    ("ДПС-П-24У (3х8)-7кН", 3, 8, "Типовая РФ (8 цветов, счётный модуль красный)"),
    ("ДПС-П-32У (4х8)-7кН", 4, 8, "Типовая РФ (8 цветов, счётный модуль красный)"),
    ("ДПС-П-48У (6х8)-7кН", 6, 8, "Типовая РФ (8 цветов, счётный модуль красный)"),
    ("ДПС-П-96У (8х12)-7кН", 8, 12, "TIA-598 (12 цветов)"),
    ("ОКЛ-24 (G.652.D)-T", 3, 8, "Типовая РФ (8 цветов, счётный модуль красный)"),
    ("ОКБ-М-48 (6х8)", 6, 8, "Типовая РФ (8 цветов, счётный модуль красный)"),
]


def upgrade() -> None:
    op.execute(FIBER_END_FUNCTION)

    conn = op.get_bind()
    conn.execute(
        sa.text("INSERT INTO length_source (code, name, default_error_pct, priority) VALUES (:c, :n, :e, :p)"),
        [dict(c=c, n=n, e=e, p=p) for c, n, e, p in LENGTH_SOURCES],
    )
    conn.execute(sa.text("INSERT INTO owner (name, short_name, color) VALUES (:n, :s, :c)"),
                 [dict(n=n, s=s, c=c) for n, s, c in OWNERS])
    conn.execute(sa.text("INSERT INTO operator (name, short_name, color) VALUES (:n, :s, :c)"),
                 [dict(n=n, s=s, c=c) for n, s, c in OPERATORS])

    for scheme_name, (fibers, modules) in COLOR_SCHEMES.items():
        scheme_id = conn.execute(sa.text("INSERT INTO color_scheme (name) VALUES (:n) RETURNING id"),
                                 dict(n=scheme_name)).scalar_one()
        rows = [dict(s=scheme_id, k="fiber", p=i, n=n, h=h) for i, (n, h) in enumerate(fibers, 1)]
        rows += [dict(s=scheme_id, k="module", p=i, n=n, h=h) for i, (n, h) in enumerate(modules, 1)]
        conn.execute(sa.text("INSERT INTO color_scheme_entry (scheme_id, kind, position, color_name, hex) "
                             "VALUES (:s, :k, :p, :n, :h)"), rows)

    conn.execute(
        sa.text("INSERT INTO cable_type (mark, modules, fibers_per_module, color_scheme_id) "
                "SELECT :m, :mo, :f, id FROM color_scheme WHERE name = :s"),
        [dict(m=m, mo=mo, f=f, s=s) for m, mo, f, s in CABLE_TYPES],
    )


def downgrade() -> None:
    op.execute("DROP TRIGGER IF EXISTS trg_splice_fiber_ends ON splice")
    op.execute("DROP TRIGGER IF EXISTS trg_cross_port_fiber_end ON cross_port")
    op.execute("DROP FUNCTION IF EXISTS splice_check_fiber_ends()")
    op.execute("DROP FUNCTION IF EXISTS cross_port_check_fiber_end()")
    op.execute("DROP FUNCTION IF EXISTS fiber_end_usage(int, varchar, int, int)")
    for table in ("cable_type", "color_scheme_entry", "color_scheme", "operator", "owner", "length_source"):
        op.execute(f"DELETE FROM {table}")
