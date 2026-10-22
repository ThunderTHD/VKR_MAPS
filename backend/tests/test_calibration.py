"""Этап 8: журнал повреждений (фактическое место, ошибка прогноза) и калибровка."""
import pytest
import sqlalchemy as sa

from app.core.db import SessionLocal
from app.core.security import hash_password
from app.models import AppUser, Cable, Incident, IncidentStatus, Role
from app.services import calibration, experiment, incidents, localization, tracing
from app.services.calib_state import current_state
from app.services.editing import EditError
from app.services.generator import GeneratorParams, generate_network, reset_network
from tests.test_localization import TW, _build


@pytest.fixture()
def net(db):
    return _build(db)


def _incident(db, net, distance):
    fiber, end, port = tracing.start_of_port(db, net["port_a"])
    tr = tracing.trace(db, fiber.id, end, start_port=port)
    res = localization.localize(tr, distance)
    return localization.save_incident(db, res, title=None, fiber_id=fiber.id, port_id=port.id, distance_m=distance,
                                      ior=None, user_id=None)


def test_actual_place_and_errors(db, net):
    inc = _incident(db, net, 250 * (1 + TW))       # прогноз: 250 м от A (участок B—C)
    assert inc.status == IncidentStatus.localized and inc.calc_details["start"] == {"port_id": net["port_a"]}
    # Фактически — на 10 м дальше по трассе (B—C идёт по долготе от 50.0016 до 50.0048, 200 м)
    lon = 50.0016 + (50.0048 - 50.0016) * (130 + 10) / 200
    incidents.set_actual(db, inc, lon, 55.00001, note="нашли в колодце")
    a = inc.calc_details["actual"]
    assert inc.status == IncidentStatus.repaired and inc.restored_at is not None
    assert a["route_error_m"] == pytest.approx(10, abs=0.5) and a["off_route_m"] < 2
    assert a["within_interval"] is (inc.predicted_error_m + 0.5 >= 10)
    assert inc.error_m == pytest.approx(10, abs=1)
    st = incidents.stats(db)
    assert st["repaired"] == 1 and st["route_error_mean_m"] == pytest.approx(10, abs=0.5)
    card = incidents.list_incidents(db)[0]
    assert card["status_ru"] == "устранено" and card["cable"] == "X"


def test_cancel_and_reopen(db, net):
    inc = _incident(db, net, 100)
    incidents.set_actual(db, inc, 50.0008, 55.0)
    incidents.update_incident(db, inc, {"status": "localized"}, None)  # ошиблись отметкой — вернуть
    assert inc.actual_geom is None and "actual" not in inc.calc_details
    incidents.update_incident(db, inc, {"status": "cancelled", "note": "ложная тревога"}, None)
    with pytest.raises(EditError, match="отменено"):
        incidents.set_actual(db, inc, 50.0008, 55.0)
    with pytest.raises(EditError):
        incidents.update_incident(db, inc, {"status": "repaired"}, None)


def test_projection_prefers_place_near_prediction():
    """Тракт дважды проходит одну линию (туда и обратно): выбирается проход у прогноза."""
    from app.services.tracing import Element, Trace
    line = [[50.0, 55.0], [50.0016, 55.0]]
    tr = Trace(elements=[
        Element("segment", 1, "X", 1, 0.0, 100, 1, "reflectometer", segment_id=1, coords=line, start_phys=0),
        Element("segment", 1, "X", 1, 0.0, 100, 1, "reflectometer", segment_id=1, coords=line[::-1],
                start_phys=100)])
    pos_first, *_ = incidents.project(tr, 50.0004, 55.0, near_pos=20)
    pos_second, *_ = incidents.project(tr, 50.0004, 55.0, near_pos=180)
    assert pos_first == pytest.approx(25, abs=1) and pos_second == pytest.approx(175, abs=1)


def test_too_few_observations(db, net):
    with pytest.raises(EditError, match="Мало данных"):
        calibration.run(db, simulate=False)


def test_calibration_improves_localization(db):
    generate_network(db, GeneratorParams(nodes=60, seed=5, buildings_per_operator=4, measured_share=1.0))
    experiment.simulate_incidents(db, n=25, seed=4)
    cr = calibration.run(db, simulate=True)
    p = cr.params
    assert p["observations"] >= 20 and p["by_kind"]["incident"] == 25
    # Генератор делает реальные трассы на 1–25 % длиннее нарисованных — поправка должна быть положительной
    assert 0.02 < p["map_stretch"] < 0.2
    assert p["holdout"]["after"]["rmse_m"] < p["holdout"]["before"]["rmse_m"]
    sim = p["simulation"]
    assert sim["after"]["error_mean_m"] < sim["before"]["error_mean_m"]
    assert sim["after"]["coverage_pct"] >= 85
    assert cr.applied_at is None and current_state(db).run_id is None  # проверка не применила калибровку
    # Применение и откат
    calibration.apply(db, cr, None)
    assert current_state(db).map_stretch == p["map_stretch"]
    calibrated = db.scalar(sa.select(sa.func.count(Cable.id)).where(Cable.twist_source == "calibration"))
    assert calibrated == sum(len(x["cables"]) for x in cr.proposals)
    with pytest.raises(EditError):
        calibration.apply(db, cr, None)
    calibration.revert(db, cr, None)
    assert current_state(db).run_id is None
    assert db.scalar(sa.select(sa.func.count(Cable.id)).where(Cable.twist_source == "calibration")) == 0


def test_calibrated_state_used_by_tracing(db, net):
    """Поправка карты меняет длину участка «по карте» и его погрешность."""
    from datetime import UTC, datetime

    from app.models import CalibrationRun
    net["segs"]["AB"].fact_length_m, net["segs"]["AB"].length_source = None, None
    db.flush()
    before = tracing.trace(db, *_start(db, net))
    db.add(CalibrationRun(params={"map_stretch": 0.1, "geo_err_pct": 4.0}, proposals=[],
                          applied_at=datetime.now(UTC)))
    db.flush()
    after = tracing.trace(db, *_start(db, net))
    e0, e1 = before.elements[0], after.elements[0]
    assert e1.length_m == pytest.approx(e0.length_m * 1.1) and e1.raw_geom_m == pytest.approx(e0.length_m)
    assert e1.err2_m == pytest.approx(e1.length_m * 0.04)


def _start(db, net):
    fiber, end, _ = tracing.start_of_port(db, net["port_a"])
    return fiber.id, end


# ------------------------------------------------------------------ API

PASSWORD = "Тест-пароль-8"
H = {"X-Requested-With": "fetch"}


@pytest.fixture()
def api_net(client):
    with SessionLocal() as s:
        ids = _build(s)
        inc = _incident(s, ids, 150)
        s.execute(sa.delete(AppUser).where(AppUser.login.in_(["c_inst", "c_view", "c_admin"])))
        for login, role in (("c_inst", Role.installer), ("c_view", Role.viewer), ("c_admin", Role.admin)):
            s.add(AppUser(login=login, full_name=login, role=role, password_hash=hash_password(PASSWORD)))
        s.commit()
        info = {"inc": inc.id}
    yield client, info
    with SessionLocal() as s:
        reset_network(s)
        s.execute(sa.text("TRUNCATE calibration_run RESTART IDENTITY"))
        s.commit()


def _login(c, who):
    assert c.post("/api/auth/login", json={"login": who, "password": PASSWORD}).status_code == 200


def test_api_journal_and_rights(api_net):
    c, ids = api_net
    _login(c, "c_view")
    j = c.get("/api/incidents").json()
    assert j["items"][0]["id"] == ids["inc"] and j["stats"]["by_status"]["localized"] == 1
    card = c.get(f"/api/objects/incident/{ids['inc']}").json()
    assert card["type"] == "incident" and any(f[0] == "Прогноз места" for f in card["fields"])
    layer = c.get("/api/map/layers/incidents").json()
    assert layer["features"][0]["properties"]["status"] == "localized"
    r = c.post(f"/api/incidents/{ids['inc']}/actual", headers=H, json={"lon": 50.001, "lat": 55.0})
    assert r.status_code == 403
    assert c.get("/api/calibration/runs").status_code == 403
    _login(c, "c_inst")
    r = c.post(f"/api/incidents/{ids['inc']}/actual", headers=H, json={"lon": 50.001, "lat": 55.0})
    assert r.status_code == 200 and r.json()["actual"]["route_error_m"] >= 0
    with SessionLocal() as s:
        assert s.get(Incident, ids["inc"]).status == IncidentStatus.repaired
    assert c.post("/api/calibration/runs", headers=H, json={}).status_code == 403
    _login(c, "c_admin")
    r = c.post("/api/calibration/runs", headers=H, json={"simulate": False})
    assert r.status_code == 409 and "Мало данных" in r.json()["detail"]
    assert c.get("/api/calibration/runs").json() == []
