"""Генератор обезличенной тестовой сети ВОЛС.

Строит правдоподобную сеть условного района: кабельная канализация по сетке кварталов,
линии опор на окраине, узлы операторов, здания-клиенты; магистральные кабели с муфтами,
ответвления к зданиям, сварки, кроссы, коммутаторы и измерения трактов.

Ключевая идея для эксперимента ВКР: генератор знает «истинную» длину каждого участка,
запаса и коэффициент укрутки каждого кабеля (таблица ``sim_truth``), а в рабочие поля
записывает значения с ошибкой, соответствующей источнику длины (рефлектометр ±1 %,
«со слов инженера» ±10 %, …). Так можно честно измерить точность локализации и эффект
калибровки: прогноз системы сравнивается с истиной, которую система не видит.

Все имена, адреса, коды и IP-адреса вымышлены. Сеть воспроизводима: одинаковый
``seed`` даёт одинаковую сеть.
"""
import math
import random
from dataclasses import dataclass, field
from datetime import date, timedelta

import networkx as nx
import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    AccessNode,
    Cable,
    CableReserve,
    CableRoute,
    CableType,
    Closure,
    CrossPort,
    FiberEnd,
    Instrument,
    InstrumentKind,
    LengthSourceCode,
    Measurement,
    MeasurementKind,
    NetworkNode,
    NodeKind,
    Operator,
    OpticalCross,
    Owner,
    RouteSegment,
    SegmentKind,
    SimTruth,
    SourceKind,
    Splice,
)
from app.services.network_ops import make_fibers, refresh_cable_geometry, refresh_segment_lengths

EARTH_M_PER_DEG = 111_320.0

# Вымышленные названия улиц для адресов зданий
STREETS = ["Лесная", "Садовая", "Речная", "Луговая", "Школьная", "Парковая", "Заводская",
           "Солнечная", "Берёзовая", "Строителей", "Молодёжная", "Рабочая"]

# Распределение источников длин участков: (источник, доля). None — длина неизвестна (только геометрия)
LENGTH_SOURCE_MIX = [
    (LengthSourceCode.reflectometer, 0.10),
    (LengthSourceCode.as_built, 0.25),
    (LengthSourceCode.passport, 0.15),
    (LengthSourceCode.engineer, 0.20),
    (None, 0.30),
]
# Относительная ошибка записанного значения (≈ 2σ) по источнику
SOURCE_ERROR = {
    LengthSourceCode.reflectometer: 0.01,
    LengthSourceCode.as_built: 0.02,
    LengthSourceCode.passport: 0.05,
    LengthSourceCode.engineer: 0.10,
}


@dataclass
class GeneratorParams:
    nodes: int = 300                 # примерное число точек сети
    seed: int = 42
    center_lat: float = 55.7030      # условный район (координаты для подложки карты)
    center_lon: float = 52.3530
    block_m: float = 170.0           # шаг сетки кварталов
    rotation_deg: float = 14.0       # поворот сетки (улицы не строго по сторонам света)
    drop_share: float = 0.15         # доля пролётов канализации, которых «нет»
    buildings_per_operator: int = 10
    measured_share: float = 0.6      # доля трактов с измерением «из ИД» (эталон для калибровки)
    # Откуда известны длины участков: {"reflectometer": 0.1, ..., "geometry": 0.3}; None — как в жизни
    # (LENGTH_SOURCE_MIX). «geometry» — длины нет, только нарисованная линия. Для эксперимента этапа 12.
    length_mix: dict[str, float] | None = None


@dataclass
class GeneratorReport:
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, key: str, n: int = 1) -> None:
        self.counts[key] = self.counts.get(key, 0) + n


class _Geo:
    """Перевод локальных метров (x — восток, y — север) в широту/долготу вокруг центра."""

    def __init__(self, lat0: float, lon0: float, rotation_deg: float):
        self.lat0, self.lon0 = lat0, lon0
        self.cos_lat = math.cos(math.radians(lat0))
        a = math.radians(rotation_deg)
        self.ca, self.sa = math.cos(a), math.sin(a)

    def to_lonlat(self, x: float, y: float) -> tuple[float, float]:
        xr, yr = x * self.ca - y * self.sa, x * self.sa + y * self.ca
        return (self.lon0 + xr / (EARTH_M_PER_DEG * self.cos_lat), self.lat0 + yr / EARTH_M_PER_DEG)


def _wkt_point(p: tuple[float, float]) -> str:
    return f"SRID=4326;POINT({p[0]:.7f} {p[1]:.7f})"


def _wkt_line(pts: list[tuple[float, float]]) -> str:
    return "SRID=4326;LINESTRING(" + ", ".join(f"{x:.7f} {y:.7f}" for x, y in pts) + ")"


def _dist_m(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Расстояние по сфере (гаверсинус), метры — для весов графа до записи в базу."""
    lon1, lat1, lon2, lat2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6_371_008.8 * math.asin(math.sqrt(h))


def _polyline_len(pts: list[tuple[float, float]]) -> float:
    return sum(_dist_m(a, b) for a, b in zip(pts, pts[1:], strict=False))


NETWORK_TABLES = [
    "sim_truth", "measurement", "incident", "node_status_history", "access_node", "splice",
    "cross_port", "optical_cross", "closure", "cable_reserve", "cable_route", "fiber", "cable",
    "jumper", "route_segment", "network_node", "instrument", "unclassified_feature",
]


def reset_network(db: Session) -> None:
    """Удалить все данные сети (справочники и пользователи остаются)."""
    db.execute(sa.text(f"TRUNCATE {', '.join(NETWORK_TABLES)} RESTART IDENTITY CASCADE"))
    db.expunge_all()  # объекты в памяти сессии больше не соответствуют строкам базы


class NetworkGenerator:
    def __init__(self, db: Session, params: GeneratorParams | None = None):
        self.db = db
        self.p = params or GeneratorParams()
        self.rnd = random.Random(self.p.seed)
        self.geo = _Geo(self.p.center_lat, self.p.center_lon, self.p.rotation_deg)
        self.report = GeneratorReport()
        self.graph = nx.Graph()      # узлы — id NetworkNode, рёбра — участки трасс
        self.coords: dict[int, tuple[float, float]] = {}
        self.true_seg_len: dict[int, float] = {}
        self.used_codes: set[str] = set()

    # ---------- вспомогательное ----------

    def _truth(self, entity: str, entity_id: int, attr: str, value: float) -> None:
        self.db.add(SimTruth(entity_type=entity, entity_id=entity_id, attribute=attr, value=value))

    def _pick_source(self) -> LengthSourceCode | None:
        r, acc = self.rnd.random(), 0.0
        mix = LENGTH_SOURCE_MIX if self.p.length_mix is None else [
            (None if k == "geometry" else LengthSourceCode(k), v) for k, v in self.p.length_mix.items()]
        for src, share in mix:
            acc += share
            if r < acc:
                return src
        return None

    def _recorded(self, true_value: float, src: LengthSourceCode) -> float:
        """Значение «как записано» с ошибкой источника (нормальное распределение, 2σ = погрешность)."""
        return round(true_value * (1 + self.rnd.gauss(0, SOURCE_ERROR[src] / 2)), 1)

    def _well_code(self) -> str:
        while True:
            code = f"к.№{self.rnd.randint(100, 999)}"
            if code not in self.used_codes:
                self.used_codes.add(code)
                return code

    def _add_node(self, kind: NodeKind, xy: tuple[float, float], **kw) -> NetworkNode:
        lonlat = self.geo.to_lonlat(*xy)
        node = NetworkNode(kind=kind, geom=_wkt_point(lonlat), source_kind=SourceKind.generator, **kw)
        self.db.add(node)
        self.db.flush()
        self.coords[node.id] = lonlat
        self.graph.add_node(node.id)
        self.report.add(f"узлы: {kind.value}")
        return node

    def _add_segment(self, kind: SegmentKind, a: NetworkNode, b: NetworkNode, owner: Owner | None,
                     stretch: tuple[float, float], bend: bool = False) -> RouteSegment:
        pa, pb = self.coords[a.id], self.coords[b.id]
        pts = [pa, pb]
        if bend:  # небольшой излом посередине — трасса редко идёт идеально прямо
            mx, my = (pa[0] + pb[0]) / 2, (pa[1] + pb[1]) / 2
            off = self.rnd.uniform(-0.00008, 0.00008)
            pts = [pa, (mx + off, my - off / 2), pb]
        geom_len = _polyline_len(pts)
        true_len = geom_len * self.rnd.uniform(*stretch)
        src = self._pick_source()
        seg = RouteSegment(
            kind=kind, owner_id=owner.id if owner else None, node_a_id=a.id, node_b_id=b.id,
            geom=_wkt_line(pts), geom_length_m=round(geom_len, 2),
            fact_length_m=self._recorded(true_len, src) if src else None, length_source=src,
            source_kind=SourceKind.generator,
        )
        self.db.add(seg)
        self.db.flush()
        self.true_seg_len[seg.id] = true_len
        self._truth("route_segment", seg.id, "length_m", true_len)
        self.graph.add_edge(a.id, b.id, segment_id=seg.id, weight=geom_len)
        self.report.add(f"участки: {kind.value}")
        return seg

    # ---------- 1. инфраструктура ----------

    def build_infrastructure(self) -> None:
        owners = {o.short_name: o for o in self.db.scalars(sa.select(Owner))}
        p = self.p
        # Сетка колодцев: ~70 % узлов — колодцы
        side = max(4, round(math.sqrt(p.nodes * 0.7)))
        half = (side - 1) / 2
        grid: dict[tuple[int, int], NetworkNode] = {}
        for i in range(side):
            for j in range(side):
                x = (i - half) * p.block_m + self.rnd.uniform(-25, 25)
                y = (j - half) * p.block_m + self.rnd.uniform(-25, 25)
                grid[i, j] = self._add_node(NodeKind.well, (x, y), code=self._well_code())
        self.grid, self.side = grid, side

        # Владелец канализации зависит от района: так на карте видны «зоны» владельцев
        def duct_owner(i: int, j: int) -> Owner:
            if i < side * 0.25:
                return owners["Телесет"]
            if j > side * 0.75:
                return owners["ВымпелКом"]
            if i > side * 0.8 and j < side * 0.3:
                return owners["ТВТ"]
            return owners["Таттелеком"]

        candidates = []
        for (i, j), node in grid.items():
            for di, dj in ((1, 0), (0, 1)):
                if (i + di, j + dj) in grid:
                    candidates.append((node, grid[i + di, j + dj], duct_owner(i, j)))
        # Часть пролётов отсутствует, но сеть должна остаться связной
        self.rnd.shuffle(candidates)
        g = nx.Graph()
        g.add_nodes_from(n.id for n in grid.values())
        g.add_edges_from((a.id, b.id) for a, b, _ in candidates)
        drop_target = int(len(candidates) * p.drop_share)
        kept = []
        for a, b, owner in candidates:
            if drop_target > 0:
                g.remove_edge(a.id, b.id)
                if nx.is_connected(g):
                    drop_target -= 1
                    continue
                g.add_edge(a.id, b.id)
            kept.append((a, b, owner))
        for a, b, owner in kept:
            self._add_segment(SegmentKind.duct, a, b, owner, stretch=(1.03, 1.12), bend=self.rnd.random() < 0.3)

        # Линии опор на восточной окраине (подвес по опорам, владелец опор — энергетики)
        self.pole_lines: list[list[NetworkNode]] = []
        for line_no, j in enumerate(sorted(self.rnd.sample(range(1, side - 1), k=min(3, side - 2))), 1):
            start = grid[side - 1, j]
            sx, sy = self._local_xy(start)
            prev, line = start, []
            for k in range(1, self.rnd.randint(7, 11)):
                xy = (sx + k * self.rnd.uniform(38, 50), sy + self.rnd.uniform(-8, 8) + k * 3)
                pole = self._add_node(NodeKind.pole, xy, code=f"оп.{line_no}-{k}", owner_id=owners["КамГЭС"].id,
                                      note="Опора ж/б 9 м")
                kind = SegmentKind.facade if prev is start else SegmentKind.aerial
                self._add_segment(kind, prev, pole, owners["КамГЭС"] if kind == SegmentKind.aerial else None,
                                  stretch=(1.01, 1.04))
                prev = pole
                line.append(pole)
            self.pole_lines.append(line)

    def _local_xy(self, node: NetworkNode) -> tuple[float, float]:
        """Обратный перевод координат узла в локальные метры (до поворота)."""
        lon, lat = self.coords[node.id]
        xr = (lon - self.p.center_lon) * EARTH_M_PER_DEG * self.geo.cos_lat
        yr = (lat - self.p.center_lat) * EARTH_M_PER_DEG
        return xr * self.geo.ca + yr * self.geo.sa, -xr * self.geo.sa + yr * self.geo.ca

    def _attach(self, kind: NodeKind, near: NetworkNode, seg_kind: SegmentKind, dist: tuple[float, float],
                **kw) -> NetworkNode:
        """Новый узел рядом с существующим и короткий участок до него (ввод в здание, узел связи)."""
        x, y = self._local_xy(near)
        ang = self.rnd.uniform(0, 2 * math.pi)
        r = self.rnd.uniform(*dist)
        node = self._add_node(kind, (x + r * math.cos(ang), y + r * math.sin(ang)), **kw)
        self._add_segment(seg_kind, near, node, None, stretch=(1.05, 1.25))
        return node

    # ---------- 2. кабельная сеть оператора ----------

    def _path_segments(self, nodes: list[int]) -> list[tuple[int, int]]:
        """Список (segment_id, direction) вдоль пути из узлов."""
        result = []
        for u, v in zip(nodes, nodes[1:], strict=False):
            seg_id = self.graph.edges[u, v]["segment_id"]
            seg = self.db.get(RouteSegment, seg_id)
            result.append((seg_id, 1 if seg.node_a_id == u else -1))
        return result

    def _lay_cable(self, op: Operator, ctype: CableType, name: str, path_nodes: list[int],
                   twist_true: float) -> Cable:
        cable = Cable(name=name, operator_id=op.id, cable_type_id=ctype.id, twist_factor=0.005,
                      start_node_id=path_nodes[0], end_node_id=path_nodes[-1],
                      lay_year=self.rnd.randint(2012, 2026), source_kind=SourceKind.generator)
        cable.fibers = make_fibers(ctype)
        cable.route = [CableRoute(seq=i, segment_id=s, direction=d)
                       for i, (s, d) in enumerate(self._path_segments(path_nodes), 1)]
        self.db.add(cable)
        self.db.flush()
        self._truth("cable", cable.id, "twist_factor", twist_true)
        self.report.add("кабели")
        self.report.add("волокна", len(cable.fibers))
        return cable

    def _reserve(self, cable: Cable, node_id: int, rng: tuple[float, float]) -> None:
        true_len = round(self.rnd.uniform(*rng), 1)
        src = self.rnd.choice([LengthSourceCode.as_built, LengthSourceCode.passport, LengthSourceCode.engineer])
        res = CableReserve(cable_id=cable.id, node_id=node_id, length_m=max(1.0, self._recorded(true_len, src)),
                           length_source=src)
        self.db.add(res)
        self.db.flush()
        self._truth("cable_reserve", res.id, "length_m", true_len)
        self.report.add("запасы")

    def build_operator(self, op: Operator, op_index: int, types: dict[str, CableType],
                       agg_ip_prefix: str, otdr: Instrument) -> None:
        side, grid = self.side, self.grid
        # Узел оператора — у колодца в «своём» квадранте центра
        qi, qj = [(0.4, 0.4), (0.6, 0.4), (0.4, 0.6), (0.6, 0.6)][op_index % 4]
        hub_well = grid[round(qi * (side - 1)), round(qj * (side - 1))]
        site = self._attach(NodeKind.site, hub_well, SegmentKind.indoor, (15, 30),
                            code=f"5{self.rnd.randint(1000, 9999)}", name=f"Узел {op.short_name} №{op_index + 1}",
                            address=f"ул. {self.rnd.choice(STREETS)}, д. {self.rnd.randint(1, 90)}")
        site_odf = OpticalCross(node_id=site.id, name="ODF-1", model="Кросс 19\" 1U, 96 портов", port_count=96,
                                connector="SC/UPC", operator_id=op.id, source_kind=SourceKind.generator)
        site_odf.ports = [CrossPort(port_no=k) for k in range(1, 97)]
        self.db.add(site_odf)
        agg = AccessNode(node_id=site.id, operator_id=op.id, hostname=f"TST-{op_index + 1}-AGG-01",
                         ip=f"{agg_ip_prefix}.0.1", model="Агрегирующий коммутатор (тест)",
                         address=site.address, source_kind=SourceKind.generator)
        self.db.add(agg)
        self.db.flush()
        self.report.add("кроссы")

        # Магистраль: от узла к дальнему краю, разрезанная муфтами на 3–5 участков
        targets = [grid[0, 0], grid[side - 1, 0], grid[0, side - 1], grid[side - 1, side - 1]]
        target = targets[(op_index + 1) % 4]
        trunk_nodes = nx.shortest_path(self.graph, site.id, target.id, weight="weight")
        cut_count = self.rnd.randint(3, 5)
        cut_idx = sorted(self.rnd.sample(range(2, len(trunk_nodes) - 1), k=min(cut_count, len(trunk_nodes) - 3)))
        bounds = [0, *cut_idx, len(trunk_nodes) - 1]
        trunk_type = types["ДПС-П-96У (8х12)-7кН"] if op_index % 2 == 0 else types["ДПС-П-48У (6х8)-7кН"]
        drop_type = self.rnd.choice([types["ОКЛ-8 (G.652.D)-T"], types["ДПО-П-08У (2х4)-300Н"]])
        twist_trunk = self.rnd.uniform(0.004, 0.012)

        trunks: list[Cable] = []
        closures: list[Closure] = []
        for k in range(len(bounds) - 1):
            part = trunk_nodes[bounds[k]: bounds[k + 1] + 1]
            cable = self._lay_cable(op, trunk_type, f"ТСТ-{op_index + 1:02d}/{k + 1:02d} магистраль",
                                    part, twist_trunk)
            trunks.append(cable)
            if k > 0:
                clo = Closure(node_id=part[0], number=f"М{op_index + 1}-{k}", model="МОГ-У (тест)",
                              operator_id=op.id, source_kind=SourceKind.generator)
                self.db.add(clo)
                self.db.flush()
                closures.append(clo)
                self.report.add("муфты")
        # Запасы: в узле и с обеих сторон каждой муфты
        self._reserve(trunks[0], site.id, (15, 25))
        for k, clo in enumerate(closures):
            self._reserve(trunks[k], clo.node_id, (10, 30))
            self._reserve(trunks[k + 1], clo.node_id, (10, 30))

        # Волокна магистрали заводятся на кросс узла по порядку
        for port, fiber in zip(site_odf.ports, trunks[0].fibers, strict=False):
            port.fiber_id, port.fiber_end = fiber.id, FiberEnd.a
        self.db.flush()

        # Сквозные сварки и ответвления к зданиям в каждой муфте
        next_fiber = 1  # следующий свободный номер волокна магистрали для ответвления
        building_no = 0
        drops_per_closure = max(1, self.p.buildings_per_operator // max(1, len(closures)))
        for k, clo in enumerate(closures):
            incoming, outgoing = trunks[k], trunks[k + 1]
            dropped: set[int] = set()
            for _ in range(drops_per_closure):
                if next_fiber > len(incoming.fibers):
                    break
                building_no += 1
                fiber_no = next_fiber
                next_fiber += 1
                dropped.add(fiber_no)
                self._build_drop(op, op_index, clo, incoming.fibers[fiber_no - 1], drop_type, building_no,
                                 site_odf.ports[fiber_no - 1], agg, agg_ip_prefix, otdr, twist_trunk)
            # Остальные волокна свариваются «насквозь»: волокно N входящего ↔ волокно N исходящего
            for f_in, f_out in zip(incoming.fibers, outgoing.fibers, strict=False):
                if f_in.number in dropped:
                    continue
                self.db.add(Splice(closure_id=clo.id, fiber_a_id=f_in.id, end_a=FiberEnd.b,
                                   fiber_b_id=f_out.id, end_b=FiberEnd.a,
                                   loss_splicer_db=round(self.rnd.uniform(0.01, 0.06), 2), note="транзит"))
                self.report.add("сварки")
        self.db.flush()

    def _build_drop(self, op: Operator, op_index: int, clo: Closure, trunk_fiber, drop_type: CableType,
                    n: int, site_port: CrossPort, agg: AccessNode, ip_prefix: str, otdr: Instrument,
                    twist_trunk: float) -> None:
        """Ответвление: муфта → здание; кросс и коммутатор в здании; при желании — измерение тракта."""
        # Здание у ближайшего к муфте колодца или опоры, не далее 3 пролётов
        candidates = [v for v, d in nx.single_source_shortest_path_length(self.graph, clo.node_id, cutoff=3).items()
                      if d >= 1 and self.db.get(NetworkNode, v).kind in (NodeKind.well, NodeKind.pole)]
        near = self.db.get(NetworkNode, self.rnd.choice(candidates) if candidates else clo.node_id)
        building = self._attach(
            NodeKind.building, near, SegmentKind.facade if near.kind == NodeKind.pole else SegmentKind.duct,
            (20, 60), address=f"ул. {self.rnd.choice(STREETS)}, д. {self.rnd.randint(1, 120)}",
            name=f"Клиент {op.short_name} {n}",
        )
        path = nx.shortest_path(self.graph, clo.node_id, building.id, weight="weight")
        twist_drop = self.rnd.uniform(0.003, 0.010)
        drop = self._lay_cable(op, drop_type, f"ТСТ-{op_index + 1:02d}/О{n:02d} ответвление", path, twist_drop)
        self._reserve(drop, clo.node_id, (5, 15))
        self._reserve(drop, building.id, (3, 10))
        self.db.add(Splice(closure_id=clo.id, fiber_a_id=trunk_fiber.id, end_a=FiberEnd.b,
                           fiber_b_id=drop.fibers[0].id, end_b=FiberEnd.a,
                           loss_splicer_db=round(self.rnd.uniform(0.01, 0.06), 2), note=building.address))
        self.report.add("сварки")
        odf = OpticalCross(node_id=building.id, name="ODF-1", model="Кросс настенный 8 портов", port_count=8,
                           connector="SC/UPC", operator_id=op.id, source_kind=SourceKind.generator)
        odf.ports = [CrossPort(port_no=k) for k in range(1, 9)]
        self.db.add(odf)
        self.db.flush()
        odf.ports[0].fiber_id, odf.ports[0].fiber_end = drop.fibers[0].id, FiberEnd.b
        self.report.add("кроссы")
        self.db.add(AccessNode(
            node_id=building.id, operator_id=op.id, hostname=f"TST-{op_index + 1}-ACC-{n:03d}",
            ip=f"{ip_prefix}.{n}.2", model="Коммутатор доступа (тест)", address=building.address,
            uplink_id=agg.id, uplink_port=f"ge0/0/{n}", cross_port_id=odf.ports[0].id,
            source_kind=SourceKind.generator,
        ))
        self.report.add("узлы доступа")
        self.db.flush()
        if self.rnd.random() < self.p.measured_share:
            self._measure(site_port, odf.ports[0], otdr)

    def _measure(self, port_a: CrossPort, port_b: CrossPort, otdr: Instrument) -> None:
        """Измерение тракта «как в ИД»: истинная оптическая длина + погрешность прибора.

        Истинная длина считается после записи всех участков (см. finalize), поэтому здесь
        только создаём запись; значение заполнит _fill_measurements.
        """
        m = Measurement(kind=MeasurementKind.otdr, port_a_id=port_a.id, port_b_id=port_b.id, wavelength_nm=1310,
                        refractive_index=1.4683, instrument_id=otdr.id,
                        measured_at=date(2026, 7, 1) + timedelta(days=self.rnd.randint(0, 90)),
                        source_kind=SourceKind.generator, note="Сгенерировано: тракт узел → здание")
        self.db.add(m)
        self.report.add("измерения")

    # ---------- 3. итог ----------

    def _true_optical_length(self, port_a_id: int, port_b_id: int, truth: dict) -> float | None:
        """Истинная оптическая длина тракта: проход по сваркам от порта A до порта B."""
        port_a = self.db.get(CrossPort, port_a_id)
        target_port = self.db.get(CrossPort, port_b_id)
        fiber, end = port_a.fiber, port_a.fiber_end
        total = 0.0
        for _ in range(50):  # защита от зацикливания
            cable = fiber.cable
            twist = truth[("cable", cable.id, "twist_factor")]
            phys = sum(truth[("route_segment", r.segment_id, "length_m")] for r in cable.route)
            phys += sum(truth[("cable_reserve", r.id, "length_m")] for r in cable.reserves)
            total += phys * (1 + twist)
            far_end = FiberEnd.b if end == FiberEnd.a else FiberEnd.a
            if target_port.fiber_id == fiber.id and target_port.fiber_end == far_end:
                return total
            sp = self.db.scalars(sa.select(Splice).where(
                sa.or_(sa.and_(Splice.fiber_a_id == fiber.id, Splice.end_a == far_end),
                       sa.and_(Splice.fiber_b_id == fiber.id, Splice.end_b == far_end)))).first()
            if sp is None:
                return None
            if sp.fiber_a_id == fiber.id and sp.end_a == far_end:
                fiber, end = sp.fiber_b, sp.end_b
            else:
                fiber, end = sp.fiber_a, sp.end_a
        return None

    def _fill_measurements(self) -> None:
        self.db.flush()
        truth = {(t.entity_type, t.entity_id, t.attribute): t.value for t in self.db.scalars(sa.select(SimTruth))}
        measurements = self.db.scalars(
            sa.select(Measurement).where(Measurement.source_kind == SourceKind.generator)).all()
        for m in measurements:
            true_len = self._true_optical_length(m.port_a_id, m.port_b_id, truth)
            if true_len is None:
                continue
            self._truth("measurement", m.id, "optical_length_m", true_len)
            m.optical_length_m = round(true_len + self.rnd.gauss(0, 1.0), 1)  # прибор: ~±2 м
            m.loss_ab_db = round(true_len / 1000 * 0.34 + self.rnd.uniform(0.1, 0.6), 3)
            m.loss_ba_db = round(m.loss_ab_db + self.rnd.uniform(-0.03, 0.03), 3)

    def run(self) -> GeneratorReport:
        types = {t.mark: t for t in self.db.scalars(sa.select(CableType))}
        operators = list(self.db.scalars(sa.select(Operator).order_by(Operator.id)))
        if not types or not operators:
            raise RuntimeError("Справочники пусты — сначала примените миграции (alembic upgrade head)")
        otdr = Instrument(kind=InstrumentKind.otdr, model="Рефлектометр (тестовый)", registry_no="00000-00",
                          serial=f"TST-{self.p.seed}", calibration_valid_until=date(2027, 6, 30))
        self.db.add(otdr)
        self.build_infrastructure()
        for idx, op in enumerate(operators):
            self.build_operator(op, idx, types, f"10.{100 + idx}", otdr)
        self.db.flush()
        refresh_segment_lengths(self.db)
        refresh_cable_geometry(self.db)
        self._fill_measurements()
        self.db.flush()
        return self.report


def generate_network(db: Session, params: GeneratorParams | None = None, reset: bool = True) -> GeneratorReport:
    if reset:
        reset_network(db)
    report = NetworkGenerator(db, params).run()
    db.commit()
    return report
