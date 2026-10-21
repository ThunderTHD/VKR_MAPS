"""Базовые операции с сетью, общие для генератора, импорта и API.

- волокна кабеля с цветами по схеме маркировки;
- пересчёт длины участка по геометрии (на эллипсоиде, через geography);
- сборка геометрии кабеля из его маршрута.
"""
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import Cable, CableType, ColorKind, Fiber


def make_fibers(cable_type: CableType) -> list[Fiber]:
    """Список волокон для кабеля данной марки с цветами модуля и волокна.

    Нумерация сквозная: модуль 1 — волокна 1..M, модуль 2 — M+1..2M и т.д.
    Цвет волокна определяется номером внутри модуля, цвет модуля — номером модуля.
    """
    scheme = cable_type.color_scheme
    fibers = []
    for module_no in range(1, cable_type.modules + 1):
        module_color = scheme.color_of(ColorKind.module, module_no) if scheme else None
        for k in range(1, cable_type.fibers_per_module + 1):
            fiber_color = scheme.color_of(ColorKind.fiber, k) if scheme else None
            fibers.append(Fiber(
                number=(module_no - 1) * cable_type.fibers_per_module + k,
                module_no=module_no,
                number_in_module=k,
                color=fiber_color.color_name if fiber_color else None,
                module_color=module_color.color_name if module_color else None,
            ))
    return fibers


def refresh_segment_lengths(db: Session, segment_ids: list[int] | None = None) -> None:
    """Пересчитать длину участков по геометрии: ST_Length(geography) — метры по эллипсоиду WGS-84."""
    sql = "UPDATE route_segment SET geom_length_m = ST_Length(geom::geography)"
    params = {}
    if segment_ids is not None:
        sql += " WHERE id = ANY(:ids)"
        params["ids"] = segment_ids
    db.execute(sa.text(sql), params)


CABLE_GEOM_SQL = """
UPDATE cable c SET geom = sub.line
FROM (
    SELECT cr.cable_id,
           ST_RemoveRepeatedPoints(ST_MakeLine(dp.geom ORDER BY cr.seq, dp.path[1])) AS line
    FROM cable_route cr
    JOIN route_segment rs ON rs.id = cr.segment_id
    CROSS JOIN LATERAL ST_DumpPoints(
        CASE WHEN cr.direction = 1 THEN rs.geom ELSE ST_Reverse(rs.geom) END) AS dp
    WHERE {where}
    GROUP BY cr.cable_id
) sub
WHERE c.id = sub.cable_id
"""


def refresh_cable_geometry(db: Session, cable_ids: list[int] | None = None) -> None:
    """Собрать линию кабеля из участков его маршрута с учётом направления прохода."""
    if cable_ids is None:
        db.execute(sa.text(CABLE_GEOM_SQL.format(where="TRUE")))
    else:
        db.execute(sa.text(CABLE_GEOM_SQL.format(where="cr.cable_id = ANY(:ids)")), {"ids": cable_ids})


def cable_fiber_count(db: Session, cable: Cable) -> int:
    return db.scalar(sa.select(sa.func.count(Fiber.id)).where(Fiber.cable_id == cable.id)) or 0
