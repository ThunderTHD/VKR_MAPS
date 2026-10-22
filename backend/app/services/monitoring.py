"""Мониторинг узлов доступа: опрос, статусы с порогом отказов, история, имитация аварий.

Режимы (MONITORING_MODE):
  * ping     — на сервере компании: ICMP-эхо на IP каждого коммутатора;
  * simulate — на ноутбуке, где сети узлов нет: ответы имитируются по «физике» сети —
    узел не отвечает, если на его линейном тракте есть оборванный участок (sim_fault)
    или у него / у вышестоящего коммутатора нет питания. Обрыв можно задать вручную
    (для демонстрации) или его время от времени создаёт сам имитатор;
  * off      — опрос выключен.

Статус «недоступен» ставится только после MONITORING_FAIL_THRESHOLD опросов подряд без
ответа (единичная потеря пакета — не авария); «доступен» — с первого ответа. Каждая смена
статуса пишется в историю (node_status_history) — из неё считается доступность и время
аварий.
"""
import logging
import random
import re
import shutil
import subprocess
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.core.config import MonitoringMode, get_settings
from app.models import AccessNode, NodeStatus, NodeStatusHistory, RouteSegment, SimFault
from app.services import impact
from app.services.editing import EditError

log = logging.getLogger("vols.monitoring")
LOCK_KEY = 74_210_011   # pg_advisory_lock: опрашивает только один процесс


@dataclass
class Probe:
    ok: bool
    rtt_ms: float | None = None


# ------------------------------------------------------------------ пробники


class PingProber:
    """ICMP-эхо. В контейнере есть fping — он опрашивает все адреса одним процессом;
    без него — системный ping по одному адресу (параллельно)."""

    RTT = re.compile(r"time[=<]\s*([\d.,]+)\s*ms")
    FPING = re.compile(r"^(\S+)\s*:\s*xmt/rcv/%loss = \d+/(\d+)/\d+%(?:, min/avg/max = [\d.]+/([\d.]+)/)?", re.M)

    def __init__(self, timeout_s: int = 1, workers: int = 32):
        self.timeout_s, self.workers = timeout_s, workers

    @staticmethod
    def available() -> bool:
        return shutil.which("ping") is not None or shutil.which("fping") is not None

    @classmethod
    def parse_fping(cls, output: str) -> dict[str, Probe]:
        """Итоги fping -q: «10.0.0.1 : xmt/rcv/%loss = 1/1/0%, min/avg/max = 0.03/0.03/0.03»."""
        return {m[1]: Probe(int(m[2]) > 0, float(m[3]) if m[3] else None) for m in cls.FPING.finditer(output)}

    def _fping(self, ips: list[str]) -> dict[str, Probe]:
        try:
            r = subprocess.run(["fping", "-q", "-c", "1", "-r", "0", "-t", str(self.timeout_s * 1000), *ips],
                               capture_output=True, text=True, timeout=self.timeout_s * 2 + len(ips) // 50 + 10)
        except (OSError, subprocess.TimeoutExpired):
            return {}
        return self.parse_fping(r.stderr + r.stdout)

    @classmethod
    def parse_rtt(cls, output: str) -> float | None:
        m = cls.RTT.search(output)
        return float(m[1].replace(",", ".")) if m else None

    def _one(self, ip: str) -> Probe:
        try:
            r = subprocess.run(["ping", "-n", "-c", "1", "-W", str(self.timeout_s), ip], capture_output=True,
                               text=True, timeout=self.timeout_s + 3)
        except (OSError, subprocess.TimeoutExpired):
            return Probe(False)
        return Probe(r.returncode == 0, self.parse_rtt(r.stdout) if r.returncode == 0 else None)

    def probe(self, db: Session, nodes: list[AccessNode]) -> dict[int, Probe]:
        targets = [(a.id, str(a.ip)) for a in nodes if a.ip is not None]
        if targets and shutil.which("fping"):
            res = self._fping([ip for _, ip in targets])
            return {i: res.get(ip, Probe(False)) for i, ip in targets}
        with ThreadPoolExecutor(self.workers) as pool:
            results = pool.map(lambda t: (t[0], self._one(t[1])), targets)
            return dict(results)


class SimProber:
    """Имитация ответов по активным имитированным авариям."""

    def __init__(self, rnd: random.Random | None = None, auto_rate: float | None = None, flap: float = 0.003):
        self.rnd = rnd or random.Random()
        self.auto_rate = get_settings().monitoring_sim_auto_rate if auto_rate is None else auto_rate
        self.flap = flap   # единичная потеря ответа — проверяет, что порог отказов работает

    def probe(self, db: Session, nodes: list[AccessNode]) -> dict[int, Probe]:
        now = datetime.now(UTC)
        db.execute(sa.delete(SimFault).where(SimFault.expires_at.is_not(None), SimFault.expires_at <= now))
        if self.auto_rate and self.rnd.random() < self.auto_rate:
            self._auto_fault(db, now)
        dead = dead_nodes(db)
        return {a.id: (Probe(False) if a.id in dead or self.rnd.random() < self.flap
                       else Probe(True, round(self.rnd.uniform(0.6, 4.5), 2))) for a in nodes}

    def _auto_fault(self, db: Session, now: datetime) -> None:
        """Самопроизвольная авария: обрыв участка, через который идёт хотя бы один тракт."""
        if db.scalar(sa.select(sa.func.count(SimFault.id)).where(SimFault.auto)):
            return
        segs = sorted({s for p in impact.service_paths(db).values() for s in p.segments})
        if not segs:
            return
        db.add(SimFault(kind="cut", segment_id=self.rnd.choice(segs), auto=True,
                        expires_at=now + timedelta(minutes=self.rnd.randint(20, 60)),
                        note="самопроизвольная авария имитатора"))
        db.flush()


def dead_nodes(db: Session) -> set[int]:
    """Узлы, которые по активным имитированным авариям не должны отвечать."""
    faults = db.scalars(sa.select(SimFault)).all()
    if not faults:
        return set()
    cut = {f.segment_id for f in faults if f.kind == "cut"}
    dead = {p.access_node_id for p in impact.service_paths(db).values() if p.segments & cut}
    for f in faults:
        if f.kind == "power":
            dead.add(f.access_node_id)
    for a in list(dead):          # подчинённые коммутаторы теряют связь через вышестоящий
        dead |= impact.downstream(db, a)
    return dead


def prober_for_mode(mode: MonitoringMode):
    if mode == MonitoringMode.ping:
        return PingProber()
    if mode == MonitoringMode.simulate:
        return SimProber()
    return None


# ------------------------------------------------------------------ опрос


def poll(db: Session, prober=None, *, threshold: int | None = None) -> dict:
    """Один опрос всех узлов: обновить статусы, записать смены статуса в историю."""
    s = get_settings()
    prober = prober or prober_for_mode(s.monitoring_mode)
    if prober is None:
        raise EditError("Мониторинг выключен (MONITORING_MODE=off)")
    threshold = threshold or s.monitoring_fail_threshold
    nodes = db.scalars(sa.select(AccessNode).order_by(AccessNode.id)).all()
    results = prober.probe(db, nodes)
    now = datetime.now(UTC)
    went_down, went_up = [], []
    for a in nodes:
        r = results.get(a.id)
        if r is None:          # без IP в режиме ping — не опрашивается
            continue
        a.last_poll_at = now
        if r.ok:
            a.fail_count, a.last_rtt_ms = 0, r.rtt_ms
            new = NodeStatus.up
        else:
            a.fail_count = min((a.fail_count or 0) + 1, 1000)
            a.last_rtt_ms = None
            new = NodeStatus.down if a.fail_count >= threshold else a.status
        if new != a.status:
            if new == NodeStatus.down:
                went_down.append(a.id)
            elif a.status == NodeStatus.down:
                went_up.append(a.id)
            a.status, a.status_changed_at = new, now
            db.add(NodeStatusHistory(access_node_id=a.id, ts=now, status=new, rtt_ms=r.rtt_ms))
    db.flush()
    counts = dict(db.execute(sa.select(AccessNode.status, sa.func.count()).group_by(AccessNode.status)).all())
    return {"polled": len(results), "at": now.isoformat(), "went_down": went_down, "went_up": went_up,
            "up": counts.get(NodeStatus.up, 0), "down": counts.get(NodeStatus.down, 0),
            "unknown": counts.get(NodeStatus.unknown, 0)}


def poll_locked(db: Session, prober=None) -> dict | None:
    """Опрос под общей блокировкой базы: два процесса не опрашивают одновременно."""
    if not db.execute(sa.text("SELECT pg_try_advisory_xact_lock(:k)"), {"k": LOCK_KEY}).scalar():
        return None
    return poll(db, prober)


class Scheduler:
    """Фоновый опрос каждые MONITORING_INTERVAL_MIN минут (поток приложения)."""

    def __init__(self, session_factory):
        self.session_factory = session_factory
        self.stop_event = threading.Event()
        self.thread: threading.Thread | None = None
        self.prober = None

    def start(self) -> None:
        s = get_settings()
        self.prober = prober_for_mode(s.monitoring_mode)
        if self.prober is None or not s.monitoring_autostart:
            return
        self.thread = threading.Thread(target=self._run, daemon=True, name="monitoring")
        self.thread.start()
        log.info("Мониторинг запущен: режим %s, интервал %s мин", s.monitoring_mode, s.monitoring_interval_min)

    def stop(self) -> None:
        self.stop_event.set()

    def _run(self) -> None:
        interval = max(get_settings().monitoring_interval_min, 1) * 60
        self.stop_event.wait(15)    # дать приложению и миграциям подняться
        while not self.stop_event.is_set():
            try:
                with self.session_factory() as db:
                    poll_locked(db, self.prober)
                    db.commit()
            except Exception:      # опрос не должен ронять приложение
                log.exception("Ошибка опроса узлов")
            self.stop_event.wait(interval)


# ------------------------------------------------------------------ имитация аварий вручную


def simulate(db: Session, *, segment_id: int | None = None, access_node_id: int | None = None,
             minutes: int | None = None, random_cut: bool = False) -> SimFault:
    if get_settings().monitoring_mode != MonitoringMode.simulate:
        raise EditError("Имитация аварий доступна только в режиме MONITORING_MODE=simulate")
    if random_cut:
        # Случайный обрыв на участке, через который идут тракты нескольких узлов, — наглядная авария
        c = Counter(sg for p in impact.service_paths(db).values() for sg in p.segments)
        shared = sorted(sg for sg, k in c.items() if k >= 2) or sorted(c)
        if not shared:
            raise EditError("В сети нет узлов доступа с заведённым трактом — обрывать нечего")
        segment_id, access_node_id = random.choice(shared), None
        note = "случайный обрыв (кнопка на странице мониторинга)"
    else:
        note = "задано вручную"
    if (segment_id is None) == (access_node_id is None):
        raise EditError("Укажите либо участок трассы (обрыв), либо узел доступа (нет питания)")
    if segment_id is not None and db.get(RouteSegment, segment_id) is None:
        raise EditError("Участок не найден")
    if access_node_id is not None and db.get(AccessNode, access_node_id) is None:
        raise EditError("Узел доступа не найден")
    f = SimFault(kind="cut" if segment_id else "power", segment_id=segment_id, access_node_id=access_node_id,
                 expires_at=datetime.now(UTC) + timedelta(minutes=minutes) if minutes else None,
                 note=note)
    db.add(f)
    db.flush()
    return f


def clear_simulation(db: Session) -> int:
    return db.execute(sa.delete(SimFault)).rowcount


def poll_until_settled(db: Session) -> dict:
    """После ручной имитации — сразу столько опросов, сколько нужно для смены статуса."""
    prober = SimProber(auto_rate=0, flap=0)
    res = None
    for _ in range(get_settings().monitoring_fail_threshold):
        res = poll(db, prober)
    return res


# ------------------------------------------------------------------ представление


def overview(db: Session, *, events: int = 50) -> dict:
    s = get_settings()
    rows = db.execute(sa.text("""
        SELECT a.id, a.hostname, host(a.ip) AS ip, a.status, a.fail_count, a.last_rtt_ms, a.status_changed_at,
               a.last_poll_at, a.uplink_id, o.short_name AS operator, a.address, a.node_id,
               ST_X(n.geom) AS x, ST_Y(n.geom) AS y
        FROM access_node a LEFT JOIN operator o ON o.id = a.operator_id
        LEFT JOIN network_node n ON n.id = a.node_id
        ORDER BY (a.status = 'down') DESC, o.short_name NULLS LAST, a.hostname""")).mappings().all()
    nodes = [{**{k: r[k] for k in ("id", "hostname", "ip", "fail_count", "last_rtt_ms", "uplink_id", "operator",
                                   "address", "node_id")},
              "status": str(r["status"]),
              "changed_at": r["status_changed_at"].isoformat() if r["status_changed_at"] else None,
              "lonlat": [r["x"], r["y"]] if r["x"] is not None else None} for r in rows]
    last_poll = max((r["last_poll_at"] for r in rows if r["last_poll_at"]), default=None)
    hist = db.execute(sa.text("""
        SELECT ts, status, id, hostname FROM (
            SELECT h.ts, h.id AS hid, h.status, a.id, a.hostname,
                   lag(h.status) OVER (PARTITION BY h.access_node_id ORDER BY h.ts, h.id) AS prev
            FROM node_status_history h JOIN access_node a ON a.id = h.access_node_id) t
        -- первое «доступен» после запуска мониторинга — не событие
        WHERE NOT (prev IS NULL AND status = 'up')
        ORDER BY ts DESC, hid DESC LIMIT :n"""),
        {"n": events}).mappings().all()
    faults = db.execute(sa.text("""
        SELECT f.id, f.kind, f.segment_id, f.access_node_id, f.created_at, f.expires_at, f.auto, f.note,
               concat_ws(' – ', coalesce(na.code, na.name), coalesce(nb.code, nb.name)) AS segment, a.hostname
        FROM sim_fault f LEFT JOIN route_segment s ON s.id = f.segment_id
        LEFT JOIN network_node na ON na.id = s.node_a_id LEFT JOIN network_node nb ON nb.id = s.node_b_id
        LEFT JOIN access_node a ON a.id = f.access_node_id ORDER BY f.id""")).mappings().all()
    return {
        "mode": s.monitoring_mode.value, "interval_min": s.monitoring_interval_min,
        "threshold": s.monitoring_fail_threshold, "last_poll_at": last_poll.isoformat() if last_poll else None,
        "counts": {k: sum(1 for n in nodes if n["status"] == k) for k in ("up", "down", "unknown")},
        "nodes": nodes,
        "events": [{"ts": h["ts"].isoformat(), "status": str(h["status"]), "id": h["id"], "hostname": h["hostname"]}
                   for h in hist],
        "sim_faults": [{"id": f["id"], "kind": f["kind"], "segment_id": f["segment_id"], "segment": f["segment"],
                        "access_node_id": f["access_node_id"], "hostname": f["hostname"], "auto": f["auto"],
                        "note": f["note"], "created_at": f["created_at"].isoformat(),
                        "expires_at": f["expires_at"].isoformat() if f["expires_at"] else None} for f in faults],
    }


def availability(db: Session, access_node_id: int, hours: int = 24) -> dict:
    """Доступность узла за период по истории смен статуса (доля времени «доступен»)."""
    end = datetime.now(UTC)
    start = end - timedelta(hours=hours)
    a = db.get(AccessNode, access_node_id)
    if a is None:
        raise EditError("Узел доступа не найден")
    prev = db.execute(sa.select(NodeStatusHistory.status).where(
        NodeStatusHistory.access_node_id == a.id, NodeStatusHistory.ts < start)
        .order_by(NodeStatusHistory.ts.desc()).limit(1)).scalar()
    changes = db.execute(sa.select(NodeStatusHistory.ts, NodeStatusHistory.status).where(
        NodeStatusHistory.access_node_id == a.id, NodeStatusHistory.ts >= start)
        .order_by(NodeStatusHistory.ts)).all()
    known = {NodeStatus.up: 0.0, NodeStatus.down: 0.0}
    cur, t = prev, start
    intervals = []
    for ts, st in [*changes, (end, None)]:
        if cur in known:
            known[cur] += (ts - t).total_seconds()
            intervals.append({"from": t.isoformat(), "to": ts.isoformat(), "status": cur.value})
        cur, t = st, ts
    total = known[NodeStatus.up] + known[NodeStatus.down]
    return {"hours": hours, "availability_pct": round(100 * known[NodeStatus.up] / total, 2) if total else None,
            "down_minutes": round(known[NodeStatus.down] / 60, 1), "intervals": intervals,
            "outages": sum(1 for _, st in changes if st == NodeStatus.down)}
