"""Редактирование сети администратором: правка, перенос, объединение, удаление, переклассификация.

Каждая операция записывается в журнал изменений (audit_log): кто, когда, что было и что стало.
Операции выполняются в транзакции вызывающего кода — либо целиком, либо никак.
"""
from datetime import UTC, datetime
from typing import Any

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    AccessNode,
    AuditLog,
    Cable,
    CableReserve,
    Closure,
    LengthSourceCode,
    NetworkNode,
    NodeKind,
    OpticalCross,
    RouteSegment,
    SegmentKind,
    SourceKind,
    UnclassifiedFeature,
)
from app.services.network_ops import refresh_cable_geometry, refresh_segment_lengths


class EditError(ValueError):
    """Операция невозможна (объяснение — в тексте исключения)."""


def _val(v: Any) -> Any:
    return v.value if hasattr(v, "value") else v


def snapshot(obj, fields: list[str]) -> dict:
    return {f: _val(getattr(obj, f)) for f in fields}


def audit(db: Session, user_id: int | None, action: str, entity: str, entity_id: int | None,
          before: dict | None = None, after: dict | None = None, comment: str | None = None) -> None:
    db.add(AuditLog(user_id=user_id, action=action, entity_type=entity, entity_id=entity_id,
                    before=before, after=after, comment=comment))
    db.flush()


NODE_FIELDS = ["kind", "code", "name", "owner_id", "address", "note"]
SEGMENT_FIELDS = ["kind", "owner_id", "name", "fact_length_m", "length_source", "length_note", "channels_total"]


# ---------------------------------------------------------------- узлы -----

def update_node(db: Session, node: NetworkNode, changes: dict, user_id: int | None) -> NetworkNode:
    before = snapshot(node, NODE_FIELDS)
    for key, value in changes.items():
        if key not in NODE_FIELDS:
            raise EditError(f"Поле «{key}» менять нельзя")
        if key == "kind":
            value = NodeKind(value)
        if isinstance(value, str):
            value = value.strip() or None
        setattr(node, key, value)
    db.flush()
    audit(db, user_id, "update", "network_node", node.id, before, snapshot(node, NODE_FIELDS))
    return node


def _affected_cables(db: Session, segment_ids: list[int]) -> list[int]:
    if not segment_ids:
        return []
    return list(db.scalars(sa.text("SELECT DISTINCT cable_id FROM cable_route WHERE segment_id = ANY(:ids)")
                           .bindparams(ids=segment_ids)))


def move_node(db: Session, node: NetworkNode, lon: float, lat: float, user_id: int | None) -> NetworkNode:
    """Перенести узел; концы примыкающих участков переносятся вместе с ним.

    ST_SetPoint заменяет первую (индекс 0) или последнюю (-1) вершину линии, промежуточные
    вершины трассы не меняются. Затем пересчитываются длины участков и геометрия кабелей.
    """
    db.flush()
    old = db.execute(sa.text("SELECT ST_X(geom), ST_Y(geom) FROM network_node WHERE id = :id"),
                     {"id": node.id}).one()
    params = {"id": node.id, "lon": lon, "lat": lat}
    db.execute(sa.text("UPDATE network_node SET geom = ST_SetSRID(ST_MakePoint(:lon, :lat), 4326) WHERE id = :id"),
               params)
    db.execute(sa.text("""
        UPDATE route_segment SET geom = ST_SetPoint(geom, 0, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))
        WHERE node_a_id = :id"""), params)
    db.execute(sa.text("""
        UPDATE route_segment SET geom = ST_SetPoint(geom, -1, ST_SetSRID(ST_MakePoint(:lon, :lat), 4326))
        WHERE node_b_id = :id"""), params)
    seg_ids = list(db.scalars(sa.select(RouteSegment.id).where(
        sa.or_(RouteSegment.node_a_id == node.id, RouteSegment.node_b_id == node.id))))
    refresh_segment_lengths(db, seg_ids)
    refresh_cable_geometry(db, _affected_cables(db, seg_ids))
    db.expire(node)
    audit(db, user_id, "move", "network_node", node.id, {"lon": old[0], "lat": old[1]}, {"lon": lon, "lat": lat})
    return node


def merge_nodes(db: Session, keep: NetworkNode, remove: NetworkNode, user_id: int | None) -> dict:
    """Объединить дубль `remove` с узлом `keep`: все ссылки перепривязываются, дубль удаляется.

    Участки, которые после объединения стали петлёй (оба конца в одном узле), удаляются,
    если по ним не проложены кабели; иначе операция отменяется — нужно сначала разобраться
    с кабелями. Конец перепривязанного участка переносится в координаты `keep`.
    """
    if keep.id == remove.id:
        raise EditError("Нельзя объединить узел сам с собой")
    loops = list(db.scalars(sa.select(RouteSegment).where(
        sa.or_(sa.and_(RouteSegment.node_a_id == keep.id, RouteSegment.node_b_id == remove.id),
               sa.and_(RouteSegment.node_a_id == remove.id, RouteSegment.node_b_id == keep.id)))))
    used = _affected_cables(db, [s.id for s in loops])
    if used:
        raise EditError("Между узлами есть участок, по которому проложены кабели — объединение сделает его "
                        "петлёй. Сначала измените маршрут кабелей.")
    before = {"keep": snapshot(keep, NODE_FIELDS), "remove": {"id": remove.id, **snapshot(remove, NODE_FIELDS)}}
    for s in loops:
        db.delete(s)
    db.flush()

    p = {"keep": keep.id, "remove": remove.id}
    seg_ids = list(db.scalars(sa.select(RouteSegment.id).where(
        sa.or_(RouteSegment.node_a_id == remove.id, RouteSegment.node_b_id == remove.id))))
    db.execute(sa.text("""
        UPDATE route_segment SET node_a_id = :keep,
               geom = ST_SetPoint(geom, 0, (SELECT geom FROM network_node WHERE id = :keep))
        WHERE node_a_id = :remove"""), p)
    db.execute(sa.text("""
        UPDATE route_segment SET node_b_id = :keep,
               geom = ST_SetPoint(geom, -1, (SELECT geom FROM network_node WHERE id = :keep))
        WHERE node_b_id = :remove"""), p)
    for model in (Closure, OpticalCross, AccessNode):
        db.execute(sa.update(model).where(model.node_id == remove.id).values(node_id=keep.id))
    db.execute(sa.update(Cable).where(Cable.start_node_id == remove.id).values(start_node_id=keep.id))
    db.execute(sa.update(Cable).where(Cable.end_node_id == remove.id).values(end_node_id=keep.id))
    # Запасы: у кабеля в узле может быть только один запас — при совпадении складываем
    for r in list(db.scalars(sa.select(CableReserve).where(CableReserve.node_id == remove.id))):
        other = db.scalar(sa.select(CableReserve).where(CableReserve.node_id == keep.id,
                                                        CableReserve.cable_id == r.cable_id))
        if other:
            other.length_m += r.length_m
            db.delete(r)
        else:
            r.node_id = keep.id
    # Пустые поля сохраняемого узла дополняются из дубля
    for f in ("code", "name", "owner_id", "address", "note"):
        if getattr(keep, f) is None and getattr(remove, f) is not None:
            setattr(keep, f, getattr(remove, f))
    if keep.kind == NodeKind.unknown and remove.kind != NodeKind.unknown:
        keep.kind = remove.kind
    db.flush()
    db.delete(remove)
    db.flush()
    refresh_segment_lengths(db, seg_ids)
    refresh_cable_geometry(db, _affected_cables(db, seg_ids))
    result = {"kept": keep.id, "removed": before["remove"]["id"], "segments_relinked": len(seg_ids),
              "loops_removed": len(loops)}
    audit(db, user_id, "merge", "network_node", keep.id, before, {**snapshot(keep, NODE_FIELDS), **result})
    return result


def delete_node(db: Session, node: NetworkNode, user_id: int | None) -> None:
    refs = {
        "участки трасс": sa.select(sa.func.count(RouteSegment.id)).where(
            sa.or_(RouteSegment.node_a_id == node.id, RouteSegment.node_b_id == node.id)),
        "муфты": sa.select(sa.func.count(Closure.id)).where(Closure.node_id == node.id),
        "кроссы": sa.select(sa.func.count(OpticalCross.id)).where(OpticalCross.node_id == node.id),
        "узлы доступа": sa.select(sa.func.count(AccessNode.id)).where(AccessNode.node_id == node.id),
        "запасы кабеля": sa.select(sa.func.count(CableReserve.id)).where(CableReserve.node_id == node.id),
    }
    used = [f"{name}: {n}" for name, q in refs.items() if (n := db.scalar(q))]
    if used:
        raise EditError("Узел используется (" + ", ".join(used) + ") — удалите или перенесите связанные объекты, "
                        "либо объедините узел с другим")
    audit(db, user_id, "delete", "network_node", node.id, snapshot(node, NODE_FIELDS))
    db.delete(node)
    db.flush()


# ---------------------------------------------------------------- участки -----

def update_segment(db: Session, seg: RouteSegment, changes: dict, user_id: int | None,
                   allowed: set[str] | None = None) -> RouteSegment:
    """Правка участка. Монтажнику разрешено менять только длину и её источник (allowed)."""
    before = snapshot(seg, SEGMENT_FIELDS)
    for key, value in changes.items():
        if key not in SEGMENT_FIELDS or (allowed is not None and key not in allowed):
            raise EditError(f"Поле «{key}» менять нельзя")
        if key == "kind":
            value = SegmentKind(value)
        if key == "length_source" and value is not None:
            value = LengthSourceCode(value)
        if isinstance(value, str):
            value = value.strip() or None
        setattr(seg, key, value)
    if "fact_length_m" in changes or "length_source" in changes:
        if seg.fact_length_m is not None and seg.length_source is None:
            raise EditError("Укажите источник длины: рефлектометр, ИД, паспорт или со слов инженера")
        if seg.fact_length_m is not None and seg.fact_length_m <= 0:
            raise EditError("Длина должна быть больше нуля")
        if seg.fact_length_m is None:
            seg.length_source = None
        seg.length_updated_by = user_id
        seg.length_updated_at = datetime.now(UTC)
    db.flush()
    audit(db, user_id, "update", "route_segment", seg.id, before, snapshot(seg, SEGMENT_FIELDS))
    return seg


def delete_segment(db: Session, seg: RouteSegment, user_id: int | None) -> None:
    if _affected_cables(db, [seg.id]):
        raise EditError("По участку проложены кабели — сначала измените их маршрут")
    audit(db, user_id, "delete", "route_segment", seg.id, snapshot(seg, SEGMENT_FIELDS))
    db.delete(seg)
    db.flush()


# ---------------------------------------------------------------- неклассифицированные -----

def classify_feature(db: Session, feat: UnclassifiedFeature, target: str, user_id: int | None,
                     node_kind: str | None = None, segment_kind: str | None = None,
                     owner_id: int | None = None, tolerance_m: float = 10.0) -> dict:
    """Превратить неклассифицированный объект в узел (точка) или участок (линия).

    Для линии концы привязываются к ближайшим узлам в пределах допуска, иначе создаются
    «неизвестные точки».
    """
    db.flush()
    gtype = db.execute(sa.text("SELECT GeometryType(geom) FROM unclassified_feature WHERE id = :id"),
                       {"id": feat.id}).scalar_one()
    result: dict = {}
    if target == "node":
        if gtype != "POINT":
            raise EditError("В узел можно превратить только точку")
        node = NetworkNode(kind=NodeKind(node_kind or "unknown"), name=feat.name, owner_id=owner_id,
                           geom=sa.select(UnclassifiedFeature.geom).where(UnclassifiedFeature.id == feat.id)
                           .scalar_subquery(), source_kind=SourceKind.kml, source_ref=feat.import_job_id)
        db.add(node)
        db.flush()
        result = {"node_id": node.id}
    elif target == "segment":
        if gtype != "LINESTRING":
            raise EditError("В участок трассы можно превратить только линию")
        ends = []
        for fn in ("ST_StartPoint", "ST_EndPoint"):
            row = db.execute(sa.text(f"""
                SELECT n.id FROM network_node n, unclassified_feature u
                WHERE u.id = :fid AND ST_DWithin(n.geom::geography, {fn}(u.geom)::geography, :tol)
                ORDER BY n.geom <-> {fn}(u.geom) LIMIT 1"""), {"fid": feat.id, "tol": tolerance_m}).first()
            if row:
                ends.append(row[0])
            else:
                node_id = db.execute(sa.text(f"""
                    INSERT INTO network_node (kind, name, geom, source_kind, source_ref, created_at, updated_at)
                    SELECT 'unknown', NULL, {fn}(geom), 'kml', import_job_id, now(), now()
                    FROM unclassified_feature WHERE id = :fid RETURNING id"""), {"fid": feat.id}).scalar_one()
                ends.append(node_id)
        if ends[0] == ends[1]:
            raise EditError("Оба конца линии попадают в одну точку")
        seg_id = db.execute(sa.text("""
            INSERT INTO route_segment (kind, owner_id, node_a_id, node_b_id, geom, geom_length_m, name,
                                       source_kind, source_ref, created_at, updated_at)
            SELECT :kind, :owner, :a, :b,
                   ST_SetPoint(ST_SetPoint(u.geom, 0, na.geom), -1, nb.geom), 0, u.name, 'kml', u.import_job_id,
                   now(), now()
            FROM unclassified_feature u, network_node na, network_node nb
            WHERE u.id = :fid AND na.id = :a AND nb.id = :b RETURNING id"""),
            {"kind": SegmentKind(segment_kind or "duct").value, "owner": owner_id, "a": ends[0], "b": ends[1],
             "fid": feat.id}).scalar_one()
        refresh_segment_lengths(db, [seg_id])
        result = {"segment_id": seg_id, "node_a_id": ends[0], "node_b_id": ends[1]}
    else:
        raise EditError("Тип назначения: node или segment")
    audit(db, user_id, "classify", "unclassified_feature", feat.id,
          {"folder": feat.folder, "name": feat.name}, {"target": target, **result})
    db.delete(feat)
    db.flush()
    return result
