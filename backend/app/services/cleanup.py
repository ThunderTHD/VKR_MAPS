"""Удаление тестовых данных генератора.

Удаляются только объекты с source_kind = 'generator'. Импортированные (KML, ИД) и введённые
вручную данные остаются. Если к сгенерированному узлу привязан «настоящий» объект
(например, линия из KML привязалась к тестовому колодцу), такой узел не удаляется — он
попадает в отчёт, чтобы администратор разобрался вручную.
"""
import sqlalchemy as sa
from sqlalchemy.orm import Session

GEN = "'generator'"

STEPS: list[tuple[str, str]] = [
    ("истинные значения", "DELETE FROM sim_truth"),
    ("измерения", f"DELETE FROM measurement WHERE source_kind = {GEN}"),
    ("узлы доступа", f"DELETE FROM access_node WHERE source_kind = {GEN}"),
    ("кроссы", f"DELETE FROM optical_cross WHERE source_kind = {GEN}"),
    ("муфты", f"DELETE FROM closure WHERE source_kind = {GEN}"),
    ("кабели", f"DELETE FROM cable WHERE source_kind = {GEN}"),  # волокна, маршрут, запасы, сварки — каскадом
    ("участки трасс", f"""
        DELETE FROM route_segment s WHERE s.source_kind = {GEN}
          AND NOT EXISTS (SELECT 1 FROM cable_route cr WHERE cr.segment_id = s.id)"""),
    ("точки сети", f"""
        DELETE FROM network_node n WHERE n.source_kind = {GEN}
          AND NOT EXISTS (SELECT 1 FROM route_segment s WHERE n.id IN (s.node_a_id, s.node_b_id))
          AND NOT EXISTS (SELECT 1 FROM closure c WHERE c.node_id = n.id)
          AND NOT EXISTS (SELECT 1 FROM optical_cross x WHERE x.node_id = n.id)
          AND NOT EXISTS (SELECT 1 FROM access_node a WHERE a.node_id = n.id)
          AND NOT EXISTS (SELECT 1 FROM cable_reserve r WHERE r.node_id = n.id)
          AND NOT EXISTS (SELECT 1 FROM cable c WHERE n.id IN (c.start_node_id, c.end_node_id))"""),
    ("тестовые приборы", """
        DELETE FROM instrument i WHERE i.serial LIKE 'TST-%'
          AND NOT EXISTS (SELECT 1 FROM measurement m WHERE m.instrument_id = i.id)"""),
]


def clear_generated(db: Session) -> dict:
    """Удалить тестовую сеть. Возвращает, сколько чего удалено и что пришлось оставить."""
    deleted = {label: db.execute(sa.text(sql)).rowcount for label, sql in STEPS}
    kept = db.execute(sa.text(f"""
        SELECT (SELECT count(*) FROM network_node WHERE source_kind = {GEN}),
               (SELECT count(*) FROM route_segment WHERE source_kind = {GEN})""")).one()
    db.flush()
    return {"deleted": deleted, "kept_nodes": kept[0], "kept_segments": kept[1]}
