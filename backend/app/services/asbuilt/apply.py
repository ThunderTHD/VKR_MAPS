"""Пакет ИД в системе: загрузка, фоновый разбор, сопоставление с сетью, подтверждение.

Порядок работы (как и с KML — два шага):
1. Загрузка → разбор в фоне (текст сразу, сканы — OCR) → результат и замечания внутри пакета.
2. Сопоставление: система предлагает кабель (по номеру ВОЛП в названии), порты кроссов
   тракта (по hostname узлов доступа и номерам портов из акта ПНР), узел доступа.
   Пользователь поправляет выбор; по нему пакет сверяется с данными системы.
3. Подтверждение — запись выбранного в одной транзакции: измерение тракта (эталон для
   калибровки), укрутка кабеля из соотношения оптической и физической длины, приборы и
   их поверка, узел доступа, опоры с координатами, PDF как документ кабеля.
"""
import re
import threading
from datetime import datetime

import sqlalchemy as sa
from sqlalchemy.orm import Session

from app.models import (
    AccessNode,
    AsBuiltPackage,
    Cable,
    CrossPort,
    Document,
    DocumentKind,
    Instrument,
    InstrumentKind,
    Measurement,
    MeasurementKind,
    OpticalCross,
    PackageStatus,
    SourceKind,
)
from app.services import drawing, tracing
from app.services.asbuilt import parser
from app.services.editing import EditError, audit
from app.services.kml.jobs import store_upload

MAX_BYTES = 100 * 1024 * 1024
STALE_MINUTES = 30


# ------------------------------------------------------------------ загрузка и фоновый разбор


def create_package(db: Session, filename: str, data: bytes, user_id: int | None) -> AsBuiltPackage:
    if len(data) > MAX_BYTES:
        raise EditError("Файл больше 100 МБ")
    if not data.startswith(b"%PDF-"):
        raise EditError("Это не PDF: пакет ИД загружается одним PDF-файлом")
    doc = store_upload(db, filename, data, DocumentKind.asbuilt, user_id)
    doc.mime = "application/pdf"
    pkg = AsBuiltPackage(status=PackageStatus.processing, filename=filename, document_id=doc.id,
                         uploaded_by=user_id, parse_result={"progress": {"done": 0, "total": 0, "stage": "очередь"}})
    db.add(pkg)
    db.flush()
    audit(db, user_id, "upload", "asbuilt_package", pkg.id, None, {"filename": filename, "size": len(data)})
    return pkg


def process(session_factory, pkg_id: int, ocr: bool = True) -> None:
    """Разбор в фоне (отдельная сессия базы). Ход разбора виден в parse_result.progress."""
    from app.core.config import get_settings

    with session_factory() as db:
        pkg = db.get(AsBuiltPackage, pkg_id)
        path = get_settings().upload_dir / pkg.document.path

        def progress(done: int, total: int, stage: str) -> None:
            pkg.parse_result = {"progress": {"done": done, "total": total, "stage": stage}}
            db.commit()

        try:
            result = parser.parse_pdf(path, ocr=ocr, progress=progress)
        except Exception as exc:  # noqa: BLE001 — любая ошибка разбора должна дойти до пользователя
            pkg.status, pkg.error = PackageStatus.failed, f"Не удалось разобрать PDF: {exc}"
            pkg.parse_result = None
            db.commit()
            return
        s = result["summary"]
        pkg.parse_result = result
        pkg.check_report = {"findings": result["findings"]}
        pkg.volp, pkg.object_name, pkg.contractor = s["volp"], s["address"], s["contractor"]
        pkg.doc_date = datetime.fromisoformat(s["doc_date"]).date() if s["doc_date"] else None
        pkg.status = PackageStatus.parsed
        db.commit()


def start_processing(session_factory, pkg_id: int, ocr: bool = True) -> threading.Thread:
    """Запустить разбор в отдельном потоке (OCR сканов может занимать минуты)."""
    t = threading.Thread(target=process, args=(session_factory, pkg_id, ocr), daemon=True,
                         name=f"asbuilt-{pkg_id}")
    t.start()
    return t


def mark_stale(db: Session) -> None:
    """Пакеты, «зависшие» в обработке (например, приложение перезапустили), помечаются ошибкой."""
    db.execute(sa.text(f"""
        UPDATE asbuilt_package SET status = 'failed',
               error = 'Разбор прервался (приложение перезапускалось?) — загрузите файл ещё раз'
        WHERE status = 'processing' AND created_at < now() - interval '{STALE_MINUTES} minutes'"""))


# ------------------------------------------------------------------ сопоставление с сетью


def _fiber_count_from_mark(mark: str | None) -> int | None:
    m = re.search(r"-(\d{1,3})(?:\s|\(|У|$)", mark or "")
    return int(m[1]) if m else None


def _port(db: Session, node_id: int | None, cross_name: str | None, port_no) -> CrossPort | None:
    """Порт кросса с данным названием в узле (или в любом узле, если название уникально)."""
    if not cross_name or port_no in (None, ""):
        return None
    try:
        port_no = int(str(port_no).strip())
    except ValueError:
        return None
    q = sa.select(CrossPort).join(OpticalCross).where(OpticalCross.name == cross_name.strip(),
                                                      CrossPort.port_no == port_no)
    if node_id is not None:
        q = q.where(OpticalCross.node_id == node_id)
    rows = db.scalars(q).all()
    return rows[0] if len(rows) == 1 else None


def suggest(db: Session, pkg: AsBuiltPackage) -> dict:
    """Что система предлагает связать с объектами сети."""
    res = pkg.parse_result or {}
    ex = res.get("extracted", {})
    volp = (res.get("summary") or {}).get("volp")
    cables = []
    if volp:
        cables = [{"id": c.id, "name": c.name} for c in db.scalars(
            sa.select(Cable).where(Cable.name.ilike(f"%{volp}%")).order_by(Cable.name).limit(10))]
    pnr = ex.get("act_pnr", {})
    dev, up = pnr.get("device") or {}, pnr.get("uplink") or {}
    an_dev = db.scalar(sa.select(AccessNode).where(AccessNode.hostname == dev.get("hostname"))) \
        if dev.get("hostname") else None
    an_up = db.scalar(sa.select(AccessNode).where(AccessNode.hostname == up.get("hostname"))) \
        if up.get("hostname") else None
    port_a = _port(db, an_dev.node_id if an_dev else None, dev.get("cross"), dev.get("cross_port"))
    if port_a is None and an_dev is not None and an_dev.cross_port_id:
        port_a = db.get(CrossPort, an_dev.cross_port_id)
    port_b = _port(db, an_up.node_id if an_up else None, up.get("cross"), up.get("cross_port"))
    sk = ex.get("skeleton", {})
    twist = None
    if sk.get("physical_m") and sk.get("optical_m"):
        twist = sk["optical_m"] / sk["physical_m"] - 1
    return {
        "cables": cables,
        "cable_id": cables[0]["id"] if len(cables) == 1 else None,
        "port_a": _port_info(port_a), "port_b": _port_info(port_b),
        "access_node": {"hostname": dev.get("hostname"), "ip": dev.get("ip") or ex.get("act_pnr_logical", {}).get("ip"),
                        "exists_id": an_dev.id if an_dev else None, "uplink": up.get("hostname"),
                        "uplink_exists_id": an_up.id if an_up else None},
        "measurement": _measurement(ex),
        "twist": None if twist is None else round(twist, 5),
        "poles": len(ex.get("poles", {}).get("poles", [])),
        "instruments": _instruments(res),
    }


def _port_info(p: CrossPort | None) -> dict | None:
    if p is None:
        return None
    return {"id": p.id, "cross_id": p.cross_id, "cross": p.cross.name, "port_no": p.port_no,
            "node": p.cross.node.code or p.cross.node.name, "connected": p.fiber_id is not None}


def _measurement(ex: dict) -> dict | None:
    m = ex.get("otdr") or ex.get("power_meter")
    if not m or not m.get("rows"):
        return None
    r = m["rows"][0]
    optical = round(r["optical_km"] * 1000, 1) if r.get("optical_km") else None
    return {"kind": m.get("kind", "otdr"), "optical_m": optical,
            "loss_ab_db": r.get("loss_ab"), "loss_ba_db": r.get("loss_ba"), "port_a": r.get("port_a"),
            "port_b": r.get("port_b"), "wavelength_nm": m.get("wavelength_nm"),
            "refractive_index": m.get("refractive_index"), "date": m.get("date")}


def _instruments(res: dict) -> list[dict]:
    """Приборы из протоколов + сроки поверки из распознанных свидетельств."""
    seen: dict[tuple, dict] = {}
    certs = [p["data"] for p in res.get("pages", []) if p["kind"] == "calibration" and p.get("data")]
    for p in res.get("pages", []):
        for ins in (p.get("data") or {}).get("instruments", []) if isinstance(p.get("data"), dict) else []:
            key = (ins["kind"], ins.get("model"), ins.get("serial"))
            item = seen.setdefault(key, {**ins, "pages": []})
            item["pages"].append(p["page"])
    for item in seen.values():
        for c in certs:
            if (item.get("serial") and c.get("serial") == item["serial"]) or (
                    item.get("registry_no") and c.get("registry_no") == item["registry_no"]):
                item["calibration_no"], item["valid_until"] = c.get("number"), c.get("valid_until")
    return list(seen.values())


def compare(db: Session, pkg: AsBuiltPackage, cable_id: int | None, port_a_id: int | None,
            port_b_id: int | None) -> list[dict]:
    """Сверка пакета с данными системы: длины кабеля, запасы, оптическая длина тракта, ёмкость."""
    out: list[dict] = []
    ex = (pkg.parse_result or {}).get("extracted", {})
    sk, lp = ex.get("skeleton", {}), ex.get("line_passport", {})
    id_phys = sk.get("physical_m") or lp.get("total_m")

    def add(level, code, msg):
        out.append({"level": level, "code": code, "message": msg, "pages": []})

    if cable_id:
        cable = db.get(Cable, cable_id)
        if cable is None:
            raise EditError("Кабель не найден")
        route = sum(r.segment.best_length_m for r in cable.route)
        reserves = sum(r.length_m for r in cable.reserves)
        sys_phys = route + reserves
        if id_phys:
            diff = sys_phys - id_phys
            lvl = "info" if abs(diff) <= max(5.0, 0.05 * id_phys) else "warning"
            add(lvl, "SYS_LENGTH", f"Физическая длина кабеля «{cable.name}»: в системе {sys_phys:.0f} м "
                f"(трасса {route:.0f} + запасы {reserves:.0f}), в ИД {id_phys:g} м — "
                f"{'совпадает' if lvl == 'info' else f'расхождение {diff:+.0f} м'}")
        id_res = sum(sk.get("reserves_m") or []) or (lp.get("parts") or {}).get("запас кабеля")
        if id_res and abs(id_res - reserves) > 2:
            add("warning", "SYS_RESERVES", f"Запасы кабеля: в системе {reserves:.0f} м, в ИД {id_res:g} м")
        n_id = _fiber_count_from_mark(lp.get("cable"))
        if n_id and n_id != cable.cable_type.fiber_count:
            add("warning", "SYS_FIBERS", f"Ёмкость: в ИД кабель {lp.get('cable')} ({n_id} ОВ), в системе марка "
                f"{cable.cable_type.mark} ({cable.cable_type.fiber_count} ОВ)")
    meas = _measurement(ex)
    if port_a_id and meas and meas.get("optical_m"):
        pa = db.get(CrossPort, port_a_id)
        if pa is None or pa.fiber_id is None:
            add("warning", "SYS_TRACE", "Порт A в системе не подключён — тракт не проверить")
        else:
            tr = tracing.trace(db, pa.fiber_id, pa.fiber_end, start_port=pa)
            pb = db.get(CrossPort, port_b_id) if port_b_id else None
            if pb is not None and not (tr.end_state == "port" and tr.legs and tr.legs[-1].fiber_id == pb.fiber_id):
                add("error", "SYS_TRACE", f"Тракт в системе от порта {pa.port_no} «{pa.cross.name}» не приходит "
                    f"в порт {pb.port_no} «{pb.cross.name}» — рассветка в системе не совпадает с ИД")
            else:
                diff = tr.total_opt - meas["optical_m"]
                lvl = "info" if abs(diff) <= max(5.0, 0.01 * meas["optical_m"]) else "warning"
                add(lvl, "SYS_OPTICAL", f"Оптическая длина тракта: по данным системы {tr.total_opt:.0f} м, "
                    f"измерено {meas['optical_m']:g} м ({diff:+.0f} м). После подтверждения измерение "
                    "станет эталоном для калибровки.")
    return out


# ------------------------------------------------------------------ подтверждение


def confirm(db: Session, pkg: AsBuiltPackage, *, cable_id: int | None, port_a_id: int | None,
            port_b_id: int | None, measurement: bool = True, twist: bool = True, instruments: bool = True,
            access_node: bool = True, poles: bool = False, attach: bool = True,
            user_id: int | None = None) -> dict:
    if pkg.status != PackageStatus.parsed:
        raise EditError("Подтвердить можно только разобранный и ещё не подтверждённый пакет")
    res = pkg.parse_result or {}
    ex = res.get("extracted", {})
    sug = suggest(db, pkg)
    done: dict[str, object] = {}
    cable = db.get(Cable, cable_id) if cable_id else None

    ins_by_kind: dict[str, Instrument] = {}
    if instruments:
        for item in sug["instruments"]:
            ins = _upsert_instrument(db, item)
            ins_by_kind.setdefault(item["kind"], ins)
        done["instruments"] = len(sug["instruments"])

    meas = sug["measurement"]
    if measurement and meas and meas.get("optical_m"):
        if not (port_a_id and port_b_id):
            raise EditError("Для записи измерения выберите порты тракта A и Б в системе")
        m = Measurement(kind=MeasurementKind(meas["kind"]), port_a_id=port_a_id, port_b_id=port_b_id,
                        wavelength_nm=int(meas["wavelength_nm"]) if meas.get("wavelength_nm") else None,
                        refractive_index=meas.get("refractive_index"), optical_length_m=meas["optical_m"],
                        loss_ab_db=meas.get("loss_ab_db"), loss_ba_db=meas.get("loss_ba_db"),
                        instrument_id=(ins_by_kind.get(meas["kind"]).id if ins_by_kind.get(meas["kind"]) else None),
                        measured_at=parser.parse_date(meas.get("date")), package_id=pkg.id,
                        source_kind=SourceKind.asbuilt, note=f"Из пакета ИД №{pkg.id} ({pkg.filename})")
        db.add(m)
        db.flush()
        done["measurement_id"] = m.id

    if twist and cable is not None and sug["twist"] is not None:
        sk = ex.get("skeleton", {})
        if not 0 <= sug["twist"] < 0.2:
            raise EditError(f"Укрутка из ИД {sug['twist']:.4f} вне допустимых пределов — проверьте длины")
        # Длины в ИД округлены до 1 м: погрешность отношения ≈ √2·0,29 / L (2σ ≈ 0,82 / L)
        before = {"twist_factor": cable.twist_factor, "twist_source": cable.twist_source}
        cable.twist_factor = round(sug["twist"], 5)
        cable.twist_err_2s = round(0.82 / sk["physical_m"], 5)
        cable.twist_source = "as_built"
        audit(db, user_id, "update", "cable", cable.id, before,
              {"twist_factor": cable.twist_factor, "twist_source": "as_built", "package_id": pkg.id})
        done["twist"] = cable.twist_factor

    if access_node and sug["access_node"].get("hostname"):
        done["access_node_id"] = _upsert_access_node(db, ex, sug, port_a_id, cable).id

    if poles and ex.get("poles", {}).get("poles"):
        done["poles"] = _add_poles(db, ex["poles"]["poles"], user_id)

    if attach and pkg.document_id and cable is not None:
        doc = db.get(Document, pkg.document_id)
        doc.owner_type, doc.owner_id = "cable", cable.id
        doc.note = f"Пакет ИД ВОЛП {pkg.volp or ''}".strip()
        done["document_id"] = doc.id

    findings = compare(db, pkg, cable_id, port_a_id, port_b_id)
    pkg.check_report = {"findings": res.get("findings", []), "system": findings, "applied": done}
    pkg.status = PackageStatus.confirmed
    pkg.cable_id = cable_id
    db.flush()
    audit(db, user_id, "confirm", "asbuilt_package", pkg.id, None, {k: v for k, v in done.items()})
    return done


def _upsert_instrument(db: Session, item: dict) -> Instrument:
    kind = InstrumentKind(item["kind"])
    ins = db.scalar(sa.select(Instrument).where(Instrument.model == item.get("model"),
                                                Instrument.serial == item.get("serial")))
    if ins is None:
        ins = Instrument(kind=kind, model=item.get("model") or "не указан", serial=item.get("serial"))
        db.add(ins)
    ins.registry_no = item.get("registry_no") or ins.registry_no
    if item.get("valid_until"):
        ins.calibration_valid_until = parser.parse_date(item["valid_until"])
        ins.calibration_no = item.get("calibration_no") or ins.calibration_no
    db.flush()
    return ins


def _upsert_access_node(db: Session, ex: dict, sug: dict, port_a_id: int | None, cable: Cable | None) -> AccessNode:
    pnr = ex.get("act_pnr", {})  # подсеть и VLAN из «логических параметров» остаются в карточке пакета
    a = sug["access_node"]
    an = db.get(AccessNode, a["exists_id"]) if a.get("exists_id") else None
    if an is None:
        an = AccessNode(hostname=a["hostname"], source_kind=SourceKind.asbuilt)
        db.add(an)
    an.ip = a.get("ip") or an.ip
    an.model = pnr.get("model") or an.model
    an.serial = pnr.get("serial") or an.serial
    an.address = pnr.get("address") or an.address
    an.uplink_id = a.get("uplink_exists_id") or an.uplink_id
    an.uplink_port = (pnr.get("uplink") or {}).get("port") or an.uplink_port
    if port_a_id:
        an.cross_port_id = port_a_id
        an.node_id = db.get(CrossPort, port_a_id).cross.node_id
    elif an.node_id is None and cable is not None:
        an.node_id = cable.end_node_id
    if an.operator_id is None and cable is not None:
        an.operator_id = cable.operator_id
    db.flush()
    return an


def _add_poles(db: Session, poles: list[dict], user_id: int | None) -> dict:
    """Опоры из ведомости: точки сети (существующая точка в 3 м используется повторно) и пролёты между ними."""
    ids, created = [], 0
    for p in poles:
        near = drawing.nearest_node(db, p["lon"], p["lat"], drawing.SNAP_M)
        if near is None:
            code = re.sub(r"^Сущ\.?\s*", "", p.get("name") or "").strip() or None
            node = drawing.create_node(db, lon=p["lon"], lat=p["lat"], kind="pole", code=code,
                                       note=p.get("type"), user_id=user_id, allow_near=True)
            node.source_kind = SourceKind.asbuilt
            near, created = node.id, created + 1
        ids.append(near)
    spans = 0
    for a, b in zip(ids, ids[1:], strict=False):
        exists = db.scalar(sa.text("""SELECT count(*) FROM route_segment
            WHERE (node_a_id = :a AND node_b_id = :b) OR (node_a_id = :b AND node_b_id = :a)"""), {"a": a, "b": b})
        if a != b and not exists:
            seg = drawing.create_segment(db, start={"node_id": a}, end={"node_id": b}, kind="aerial", user_id=user_id)
            seg.source_kind = SourceKind.asbuilt
            spans += 1
    db.flush()
    return {"poles_created": created, "poles_reused": len(ids) - created, "spans_created": spans}


def reject(db: Session, pkg: AsBuiltPackage, reason: str | None, user_id: int | None) -> None:
    if pkg.status not in (PackageStatus.parsed, PackageStatus.failed):
        raise EditError("Отклонить можно только неподтверждённый пакет")
    pkg.status = PackageStatus.rejected
    pkg.error = (reason or "").strip() or pkg.error
    audit(db, user_id, "reject", "asbuilt_package", pkg.id, None, {"reason": reason})
    db.flush()


def package_summary(pkg: AsBuiltPackage) -> dict:
    res = pkg.parse_result or {}
    s = res.get("summary") or {}
    return {"id": pkg.id, "filename": pkg.filename, "status": pkg.status.value, "volp": pkg.volp,
            "object": pkg.object_name, "contractor": pkg.contractor,
            "doc_date": pkg.doc_date.isoformat() if pkg.doc_date else None,
            "created_at": pkg.created_at.isoformat() if pkg.created_at else None, "error": pkg.error,
            "progress": res.get("progress"), "pages": s.get("pages"), "errors": s.get("errors"),
            "warnings": s.get("warnings"), "cable_id": pkg.cable_id, "document_id": pkg.document_id}

