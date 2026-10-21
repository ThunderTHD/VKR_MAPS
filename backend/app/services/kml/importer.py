"""Импорт KML: план (предпросмотр) и запись в базу.

Импорт выполняется в два шага:
  1) plan_import — разбор файла и привязка БЕЗ записи: что будет создано, что уже есть,
     какие предупреждения. Результат показывается пользователю на карте;
  2) commit_import — тот же план строится заново (база могла измениться) и записывается
     в одной транзакции: при любой ошибке не сохраняется ничего.

Привязка концов линий к узлам — по шагам:
  а) по коду из названия линии («к.№348 - к.№498»), если узел с таким кодом есть в файле
     или в базе и находится не дальше CODE_MAX_DIST_M от конца линии;
  б) иначе — ближайший узел в пределах допуска (по умолчанию 10 м);
  в) иначе создаётся новый узел в конце линии: колодец/площадка, если код удалось разобрать,
     иначе «неизвестная точка» с текстом из названия.
Направление рисования линии может не совпадать с порядком в названии — выбирается
ориентация, при которой коды лучше совпадают с положением узлов.
"""
import math
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Closure,
    FolderTarget,
    KmlFolderRule,
    NetworkNode,
    NodeKind,
    Owner,
    RouteSegment,
    SegmentKind,
    SourceKind,
    UnclassifiedFeature,
)
from app.services.kml.names import Endpoint, closure_label, is_generic, parse_line_name, parse_point_name
from app.services.kml.parser import KmlDocument, Placemark
from app.services.network_ops import refresh_segment_lengths

CODE_MAX_DIST_M = 300.0        # код совпал, но точка дальше — это уже подозрительно
SCHEMATIC_MIN_LEN_M = 300.0    # прямая из двух точек длиннее — вероятно, схематичная линия


# ---------------------------------------------------------------- геометрия -----

def dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Расстояние между точками (lon, lat) по сфере, метры."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(min(1.0, h)))


def line_len_m(coords: list[tuple[float, float]]) -> float:
    return sum(dist_m(a, b) for a, b in zip(coords, coords[1:], strict=False))


# ---------------------------------------------------------------- сопоставление папок -----

@dataclass
class FolderMapping:
    target: str                      # FolderTarget: node | segment | closure | unclassified | skip
    node_kind: str | None = None     # для target=node
    segment_kind: str | None = None  # для target=segment
    owner_id: int | None = None


def _owner_in_text(text: str, owners: list[Owner]) -> Owner | None:
    low = text.lower()
    # Длинные названия раньше коротких: «ВымпелКом» не должен совпасть по «ТВТ»
    for o in sorted(owners, key=lambda o: -len(o.short_name)):
        if o.short_name.lower() in low:
            return o
    return None


def suggest_mapping(db: Session, doc: KmlDocument) -> dict[str, FolderMapping]:
    """Сопоставление папок по умолчанию: запомненные правила, иначе — по словам в названии."""
    owners = list(db.scalars(sa.select(Owner)))
    rules = {r.folder_name: r for r in db.scalars(sa.select(KmlFolderRule))}
    result = {}
    for name, info in doc.folders().items():
        if name in rules:
            r = rules[name]
            result[name] = FolderMapping(r.target.value, r.node_kind.value if r.node_kind else None,
                                         r.segment_kind.value if r.segment_kind else None, r.owner_id)
            continue
        low = name.lower()
        owner = _owner_in_text(name, owners)
        only_points = set(info["types"]) == {"Point"}
        only_lines = set(info["types"]) == {"LineString"}
        if only_points and ("колод" in low or "ккс" in low):
            m = FolderMapping("node", node_kind="well")
        elif only_points and "опор" in low:
            m = FolderMapping("node", node_kind="pole")
        elif only_points and "муфт" in low:
            m = FolderMapping("closure")
        elif only_lines and "микро" in low:
            m = FolderMapping("segment", segment_kind="microduct")
        elif only_lines and "канализ" in low:
            m = FolderMapping("segment", segment_kind="duct")
        elif only_lines and ("гнб" in low or "прокол" in low):
            m = FolderMapping("segment", segment_kind="hdd")
        elif only_lines and ("подвес" in low or "опор" in low or "воздуш" in low):
            m = FolderMapping("segment", segment_kind="aerial")
        else:
            m = FolderMapping("unclassified")  # например «NEC» — назначение неизвестно
        m.owner_id = owner.id if owner else None
        result[name] = m
    return result


# ---------------------------------------------------------------- план -----

@dataclass
class PlanNode:
    key: str                     # «db:15» — существующий, «new:3» — будет создан
    kind: str
    code: str | None
    name: str | None
    owner_id: int | None
    lonlat: tuple[float, float]
    status: str                  # existing | update | new | auto
    existing_id: int | None = None
    placemark: int | None = None


@dataclass
class PlanSegment:
    placemark: int
    name: str | None
    kind: str
    owner_id: int | None
    coords: list[tuple[float, float]]
    a: str
    b: str
    status: str                  # new | duplicate | skip
    length_m: float = 0.0
    warnings: list[str] = field(default_factory=list)


@dataclass
class PlanClosure:
    node: str
    number: str
    placemark: int


@dataclass
class PlanUnclassified:
    placemark: int
    folder: str
    name: str | None
    geom_type: str
    coords: list[tuple[float, float]]


@dataclass
class Warning_:
    level: str                   # warning | info | error
    code: str
    message: str
    placemark: int | None = None
    lonlat: tuple[float, float] | None = None


@dataclass
class ImportPlan:
    nodes: dict[str, PlanNode] = field(default_factory=dict)
    segments: list[PlanSegment] = field(default_factory=list)
    closures: list[PlanClosure] = field(default_factory=list)
    unclassified: list[PlanUnclassified] = field(default_factory=list)
    warnings: list[Warning_] = field(default_factory=list)
    skipped: int = 0

    def warn(self, code: str, message: str, pm: Placemark | None = None,
             lonlat: tuple[float, float] | None = None, level: str = "warning") -> None:
        self.warnings.append(Warning_(level, code, message, pm.index if pm else None,
                                      lonlat or (pm.coords[0] if pm else None)))

    def summary(self) -> dict:
        nodes = list(self.nodes.values())
        count = lambda items, st: sum(1 for x in items if x.status == st)  # noqa: E731
        return {
            "nodes_new": count(nodes, "new"), "nodes_auto": count(nodes, "auto"),
            "nodes_update": count(nodes, "update"), "nodes_existing_used": count(nodes, "existing"),
            "segments_new": count(self.segments, "new"), "segments_duplicate": count(self.segments, "duplicate"),
            "segments_skipped": count(self.segments, "skip"),
            "closures": len(self.closures), "unclassified": len(self.unclassified),
            "warnings": sum(1 for w in self.warnings if w.level == "warning"),
            "errors": sum(1 for w in self.warnings if w.level == "error"),
            "skipped_placemarks": self.skipped,
        }

    def to_json(self) -> dict:
        return {"summary": self.summary(), "nodes": [asdict(n) for n in self.nodes.values()],
                "segments": [asdict(s) for s in self.segments], "closures": [asdict(c) for c in self.closures],
                "unclassified": [asdict(u) for u in self.unclassified], "warnings": [asdict(w) for w in self.warnings]}

    def to_geojson(self) -> dict:
        """Предпросмотр для карты: узлы, участки, муфты, неклассифицированные — со статусами."""
        feats = []
        for n in self.nodes.values():
            if n.status == "existing":
                continue
            feats.append({"type": "Feature", "geometry": {"type": "Point", "coordinates": list(n.lonlat)},
                          "properties": {"layer": "node", "status": n.status, "kind": n.kind, "code": n.code,
                                         "name": n.name, "key": n.key}})
        for s in self.segments:
            line = {"type": "LineString", "coordinates": [list(c) for c in s.coords]}
            feats.append({"type": "Feature", "geometry": line,
                          "properties": {"layer": "segment", "status": "warning" if s.warnings and s.status == "new"
                                         else s.status, "kind": s.kind, "name": s.name, "placemark": s.placemark,
                                         "length_m": round(s.length_m, 1), "warnings": s.warnings,
                                         "a": self._label(s.a), "b": self._label(s.b)}})
        for u in self.unclassified:
            geom = {"type": "Point", "coordinates": list(u.coords[0])} if u.geom_type == "Point" else \
                {"type": "LineString", "coordinates": [list(c) for c in u.coords]}
            feats.append({"type": "Feature", "geometry": geom,
                          "properties": {"layer": "unclassified", "status": "unclassified", "name": u.name,
                                         "folder": u.folder, "placemark": u.placemark}})
        return {"type": "FeatureCollection", "features": feats}

    def _label(self, key: str) -> str:
        n = self.nodes[key]
        return n.code or n.name or n.kind


class Planner:
    def __init__(self, db: Session, doc: KmlDocument, mapping: dict[str, FolderMapping], tolerance_m: float):
        self.db, self.doc, self.mapping, self.tol = db, doc, mapping, tolerance_m
        self.plan = ImportPlan()
        self.owners = list(db.scalars(sa.select(Owner)))
        self._new_seq = 0
        self._by_code: dict[str, str] = {}   # код → ключ узла (файл и база)
        self._existing_pairs: set[frozenset] = set()  # пары узлов уже существующих участков
        self._load_existing()

    # ----- существующие узлы базы в районе файла -----

    def _load_existing(self) -> None:
        pts = [c for p in self.doc.placemarks for c in p.coords]
        if not pts:
            return
        pad = 0.01  # ~1 км запаса вокруг файла
        x1, y1 = min(p[0] for p in pts) - pad, min(p[1] for p in pts) - pad
        x2, y2 = max(p[0] for p in pts) + pad, max(p[1] for p in pts) + pad
        rows = self.db.execute(sa.text("""
            SELECT id, kind, code, name, owner_id, ST_X(geom), ST_Y(geom) FROM network_node
            WHERE geom && ST_MakeEnvelope(:x1, :y1, :x2, :y2, 4326)"""),
            {"x1": x1, "y1": y1, "x2": x2, "y2": y2}).all()
        for id_, kind, code, name, owner_id, x, y in rows:
            key = f"db:{id_}"
            self.plan.nodes[key] = PlanNode(key, kind, code, name, owner_id, (x, y), "existing", existing_id=id_)
            if code:
                self._by_code.setdefault(code, key)
        self._existing_pairs = {
            frozenset(r) for r in self.db.execute(sa.text("""
                SELECT node_a_id, node_b_id FROM route_segment
                WHERE geom && ST_MakeEnvelope(:x1, :y1, :x2, :y2, 4326)"""),
                {"x1": x1, "y1": y1, "x2": x2, "y2": y2}).all()}

    def _new_key(self) -> str:
        self._new_seq += 1
        return f"new:{self._new_seq}"

    def _nearest(self, pt: tuple[float, float], within: float) -> tuple[str | None, float]:
        best, best_d = None, within
        for key, n in self.plan.nodes.items():
            d = dist_m(pt, n.lonlat)
            if d <= best_d:
                best, best_d = key, d
        return best, best_d

    def _add_node(self, status: str, kind: str, code: str | None, name: str | None, owner_id: int | None,
                  lonlat: tuple[float, float], placemark: int | None = None) -> str:
        key = self._new_key()
        self.plan.nodes[key] = PlanNode(key, kind, code, name, owner_id, lonlat, status, placemark=placemark)
        if code:
            self._by_code.setdefault(code, key)
        return key

    # ----- точки -----

    def point(self, pm: Placemark, m: FolderMapping, closure: bool = False) -> None:
        pt = pm.coords[0]
        ep = parse_point_name(pm.name)
        if closure:
            # Муфта: ставится в ближайший узел или в новую точку «муфта вне колодца»
            key, _ = self._nearest(pt, self.tol)
            if key is None:
                key = self._add_node("new", NodeKind.closure_point.value, None, pm.name, m.owner_id, pt, pm.index)
            self.plan.closures.append(PlanClosure(key, closure_label(pm.name) or pm.name or f"М-{pm.index}",
                                                  pm.index))
            return
        kind = m.node_kind or NodeKind.unknown.value
        code = ep.code if ep else None
        name = None if (ep and ep.kind != "name") else (pm.name if not is_generic(pm.name) else None)
        owner = m.owner_id
        if owner is None and ep and ep.owner_hint:
            o = _owner_in_text(ep.owner_hint, self.owners)
            owner = o.id if o else None
        if ep and ep.kind == "site" and kind == NodeKind.well.value:
            kind = NodeKind.site.value

        # Уже есть узел с этим кодом?
        if code and code in self._by_code:
            key = self._by_code[code]
            n = self.plan.nodes[key]
            d = dist_m(pt, n.lonlat)
            if n.status == "existing":
                n.status = "update"
                if owner and not n.owner_id:
                    n.owner_id = owner
                if d > self.tol:
                    self.plan.warn("moved", f"{code}: в файле точка в {d:.0f} м от узла в базе — "
                                            f"координаты в базе оставлены, проверьте", pm)
            else:
                self.plan.warn("duplicate_code", f"{code}: в файле несколько точек с этим кодом "
                                                 f"(расхождение {d:.0f} м) — использована первая", pm)
            return
        key, d = self._nearest(pt, self.tol)
        if key and self.plan.nodes[key].status == "existing" and not (code and self.plan.nodes[key].code):
            # Точка совпала с узлом базы без кода — дополняем узел данными из файла
            n = self.plan.nodes[key]
            n.status = "update"
            n.code = n.code or code
            n.name = n.name or name
            n.owner_id = n.owner_id or owner
            if code:
                self._by_code.setdefault(code, key)
            return
        self._add_node("new", kind, code, name, owner, pt, pm.index)
        if not code and kind == NodeKind.well.value:
            self.plan.warn("no_code", f"Колодец без номера: «{pm.name}»", pm, level="info")

    # ----- линии -----

    def _resolve(self, pt: tuple[float, float], hint: Endpoint | None, pm: Placemark,
                 seg_owner: int | None) -> str:
        if hint and hint.code and hint.code in self._by_code:
            key = self._by_code[hint.code]
            d = dist_m(pt, self.plan.nodes[key].lonlat)
            if d <= CODE_MAX_DIST_M:
                if d > self.tol:
                    self.plan.warn("far_end", f"«{pm.name}»: конец линии в {d:.0f} м от {hint.code} "
                                              f"(привязан по коду)", pm, lonlat=pt)
                return key
            self.plan.warn("code_far", f"«{pm.name}»: {hint.code} найден, но в {d:.0f} м от конца линии — "
                                       f"привязка по расстоянию", pm, lonlat=pt)
        key, d = self._nearest(pt, self.tol)
        if key:
            n = self.plan.nodes[key]
            if hint and hint.code and n.code and n.code != hint.code:
                self.plan.warn("code_mismatch", f"«{pm.name}»: по названию {hint.code}, рядом ({d:.1f} м) — "
                                                f"{n.code}; привязан к {n.code}", pm, lonlat=pt)
            return key
        # Новый узел в конце линии
        if hint and hint.kind == "well":
            kind, code, name, owner = NodeKind.well.value, hint.code, None, seg_owner
        elif hint and hint.kind == "site":
            kind, code, name, owner = NodeKind.site.value, hint.code, None, None
        else:
            kind, code, name, owner = NodeKind.unknown.value, None, hint.label if hint else None, None
        key = self._add_node("auto", kind, code, name, owner, pt, pm.index)
        self.plan.warn("auto_node", f"Создана точка {code or name or 'без названия'} в конце линии "
                                    f"«{pm.name or 'без названия'}» — узла нет ни в файле, ни в базе",
                       pm, lonlat=pt, level="info")
        return key

    @staticmethod
    def _orientation_cost(coords, hints: list[Endpoint], nodes_by_code, plan) -> float:
        cost = 0.0
        for hint, pt in ((hints[0], coords[0]), (hints[-1], coords[-1])):
            if hint.code and hint.code in nodes_by_code:
                cost += dist_m(pt, plan.nodes[nodes_by_code[hint.code]].lonlat)
        return cost

    def line(self, pm: Placemark, m: FolderMapping) -> None:
        coords = pm.coords
        if len(coords) < 2:
            self.plan.warn("short_line", f"«{pm.name}»: линия из одной точки — пропущена", pm, level="error")
            self.plan.skipped += 1
            return
        hints = parse_line_name(pm.name)
        if is_generic(pm.name):
            self.plan.warn("untitled", "Линия без названия — концы определены только по расстоянию", pm)
        elif not hints:
            self.plan.warn("unparsed", f"«{pm.name}»: в названии нет концов — привязка по расстоянию", pm,
                           level="info")
        if len(hints) >= 2:
            fwd = self._orientation_cost(coords, hints, self._by_code, self.plan)
            rev = self._orientation_cost(coords, hints[::-1], self._by_code, self.plan)
            if rev < fwd:
                hints = hints[::-1]

        # Разрезание по промежуточным узлам («к.31-16 - к.№508 - 57177» проходит через к.№508)
        pieces = [(coords, hints[0] if hints else None, hints[-1] if hints else None)]
        if len(hints) > 2:
            pieces = self._split(pm, coords, hints)
        kind = m.segment_kind or SegmentKind.duct.value
        for part, h_start, h_end in pieces:
            a = self._resolve(part[0], h_start, pm, m.owner_id)
            b = self._resolve(part[-1], h_end, pm, m.owner_id)
            seg = PlanSegment(pm.index, pm.name, kind, m.owner_id, list(part), a, b, "new",
                              length_m=line_len_m(part))
            if a == b:
                seg.status = "skip"
                seg.warnings.append("оба конца привязаны к одной точке — участок пропущен")
                self.plan.warn("loop", f"«{pm.name}»: оба конца привязаны к одной точке — пропущен", pm)
            elif frozenset((self.plan.nodes[a].existing_id, self.plan.nodes[b].existing_id)) in self._existing_pairs:
                seg.status = "duplicate"
            elif any(s.status == "new" and {s.a, s.b} == {a, b} for s in self.plan.segments):
                seg.status = "duplicate"
                self.plan.warn("dup_in_file", f"«{pm.name}»: участок между этими точками уже есть в файле", pm)
            if len(part) == 2 and seg.length_m > SCHEMATIC_MIN_LEN_M:
                seg.warnings.append(f"прямая {seg.length_m:.0f} м — вероятно, схематичная: длина по карте ненадёжна")
            self.plan.segments.append(seg)

    def _split(self, pm: Placemark, coords, hints: list[Endpoint]):
        """Разрезать линию в вершинах, ближайших к промежуточным узлам из названия."""
        cuts = []
        for h in hints[1:-1]:
            target = self.plan.nodes.get(self._by_code.get(h.code or "", ""), None)
            if target is None:
                self.plan.warn("mid_unknown", f"«{pm.name}»: промежуточная точка {h.label} не найдена — "
                                              f"линия не разрезана", pm)
                return [(coords, hints[0], hints[-1])]
            i, d = min(((i, dist_m(c, target.lonlat)) for i, c in enumerate(coords[1:-1], 1)),
                       key=lambda t: t[1], default=(None, math.inf))
            if i is None or d > max(self.tol, 30.0):
                self.plan.warn("mid_far", f"«{pm.name}»: линия проходит в {d:.0f} м от {h.label} — не разрезана",
                               pm)
                return [(coords, hints[0], hints[-1])]
            cuts.append((i, h))
        cuts.sort(key=lambda t: t[0])
        pieces, start, h_start = [], 0, hints[0]
        for i, h in cuts:
            pieces.append((coords[start:i + 1], h_start, h))
            start, h_start = i, h
        pieces.append((coords[start:], h_start, hints[-1]))
        return pieces

    def unclassified(self, pm: Placemark) -> None:
        self.plan.unclassified.append(PlanUnclassified(pm.index, pm.folder, pm.name, pm.geom_type, pm.coords))

    def run(self) -> ImportPlan:
        # Сначала все точки (чтобы линии привязывались к ним), потом линии
        order: Iterable[Placemark] = sorted(self.doc.placemarks, key=lambda p: p.geom_type != "Point")
        for pm in order:
            m = self.mapping.get(pm.folder) or FolderMapping("unclassified")
            target = m.target
            if target == FolderTarget.skip.value:
                self.plan.skipped += 1
            elif target == FolderTarget.unclassified.value or pm.geom_type == "Polygon":
                self.unclassified(pm)
            elif target == FolderTarget.closure.value and pm.geom_type == "Point":
                self.point(pm, m, closure=True)
            elif target == FolderTarget.node.value and pm.geom_type == "Point":
                self.point(pm, m)
            elif target == FolderTarget.segment.value and pm.geom_type == "LineString":
                self.line(pm, m)
            else:
                self.plan.warn("type_mismatch", f"«{pm.name}»: {pm.geom_type} в папке «{pm.folder}», "
                                                f"назначенной как {target} — в неклассифицированные", pm)
                self.unclassified(pm)
        return self.plan


def plan_import(db: Session, doc: KmlDocument, mapping: dict[str, FolderMapping],
                tolerance_m: float = 10.0) -> ImportPlan:
    return Planner(db, doc, mapping, tolerance_m).run()


# ---------------------------------------------------------------- запись -----

def _wkt_point(p) -> str:
    return f"SRID=4326;POINT({p[0]:.8f} {p[1]:.8f})"


def _wkt_line(coords) -> str:
    return "SRID=4326;LINESTRING(" + ", ".join(f"{x:.8f} {y:.8f}" for x, y in coords) + ")"


def commit_plan(db: Session, plan: ImportPlan, job_id: int, user_id: int | None,
                mapping: dict[str, FolderMapping]) -> dict:
    """Записать план в базу (вызывающий код отвечает за commit транзакции)."""
    ids: dict[str, int] = {}
    created = {"nodes": 0, "segments": 0, "closures": 0, "unclassified": 0, "nodes_updated": 0}
    for key, n in plan.nodes.items():
        if n.existing_id is not None:
            ids[key] = n.existing_id
            if n.status == "update":
                # Дополняем только пустые поля — данные, уже введённые в системе, не затираем
                node = db.get(NetworkNode, n.existing_id)
                changed = False
                for attr in ("code", "name", "owner_id"):
                    if getattr(node, attr) is None and getattr(n, attr) is not None:
                        setattr(node, attr, getattr(n, attr))
                        changed = True
                created["nodes_updated"] += changed
            continue
        node = NetworkNode(kind=NodeKind(n.kind), code=n.code, name=n.name, owner_id=n.owner_id,
                           geom=_wkt_point(n.lonlat), source_kind=SourceKind.kml, source_ref=job_id)
        db.add(node)
        db.flush()
        ids[key] = node.id
        created["nodes"] += 1

    new_segments = []
    for s in plan.segments:
        if s.status != "new":
            continue
        # Концы участка совмещаются с узлами точно — топология без зазоров
        coords = [plan.nodes[s.a].lonlat, *s.coords[1:-1], plan.nodes[s.b].lonlat]
        seg = RouteSegment(kind=SegmentKind(s.kind), owner_id=s.owner_id, node_a_id=ids[s.a], node_b_id=ids[s.b],
                           geom=_wkt_line(coords), geom_length_m=s.length_m, name=s.name,
                           source_kind=SourceKind.kml, source_ref=job_id)
        db.add(seg)
        new_segments.append(seg)
        created["segments"] += 1
    db.flush()
    refresh_segment_lengths(db, [s.id for s in new_segments])

    for c in plan.closures:
        exists = db.scalar(sa.select(Closure.id).where(Closure.node_id == ids[c.node], Closure.number == c.number))
        if not exists:
            db.add(Closure(node_id=ids[c.node], number=c.number, source_kind=SourceKind.kml, source_ref=job_id))
            created["closures"] += 1
    for u in plan.unclassified:
        geom = _wkt_point(u.coords[0]) if u.geom_type == "Point" else _wkt_line(u.coords)
        # Повторный импорт: такой же объект (папка, название, геометрия) уже лежит в базе
        same = db.scalar(sa.select(UnclassifiedFeature.id).where(
            UnclassifiedFeature.folder == u.folder, UnclassifiedFeature.name.is_not_distinct_from(u.name),
            sa.func.ST_Equals(UnclassifiedFeature.geom, sa.func.ST_GeomFromEWKT(geom))))
        if same is None:
            db.add(UnclassifiedFeature(import_job_id=job_id, folder=u.folder, name=u.name, geom=geom))
            created["unclassified"] += 1

    # Запоминаем сопоставление папок для следующих импортов
    for folder, m in mapping.items():
        rule = db.scalar(sa.select(KmlFolderRule).where(KmlFolderRule.folder_name == folder))
        if rule is None:
            rule = KmlFolderRule(folder_name=folder)
            db.add(rule)
        rule.target = FolderTarget(m.target)
        rule.node_kind = NodeKind(m.node_kind) if m.node_kind else None
        rule.segment_kind = SegmentKind(m.segment_kind) if m.segment_kind else None
        rule.owner_id = m.owner_id
    db.add(AuditLog(user_id=user_id, action="import", entity_type="import_job", entity_id=job_id, after=created))
    db.flush()
    return created
