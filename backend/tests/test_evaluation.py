"""Этап 12: эксперимент для ВКР — серии опытов на маленьких сетях (быстро) и отчёт."""
import pytest
import sqlalchemy as sa

from app.core.config import get_settings
from app.services import evaluation

CFG = {"seeds": [5], "faults": 25, "curve": [0, 10], "cuts": 8, "sizes": [100], "nodes": 140, "incidents": 12}


@pytest.fixture(scope="module")
def run(migrated_db):
    """Весь набор серий один раз на модуль — в отдельной базе, как на странице «Эксперимент»."""
    stages = []
    res = evaluation.run_isolated(get_settings().database_url, "quick", cfg=CFG,
                                  progress=lambda s, f: stages.append((s, f)))
    res["_stages"] = stages
    return res


def test_suite_structure_and_progress(run):
    assert set(run["results"]) == {"e1", "e2", "e3", "e4", "e5"}
    fracs = [f for _, f in run["_stages"]]
    assert fracs == sorted(fracs) and fracs[-1] == 1.0


def test_e1_system_beats_manual(run):
    rows = {r["method"]: r for r in run["results"]["e1"]["rows"]}
    assert rows["system"]["median_m"] < rows["docs"]["median_m"] < rows["map"]["median_m"] + 1e-9
    assert rows["calibrated"]["median_m"] <= rows["system"]["median_m"] * 1.5
    assert 80 <= rows["system"]["coverage_pct"] <= 100
    assert rows["system"]["vs_map_pct"] > 50
    hist = run["results"]["e1"]["hist"]
    assert all(sum(v) == rows["map"]["n"] for v in hist["counts"].values())


def test_e2_more_data_less_error(run):
    rows = {r["scenario"]: r for r in run["results"]["e2"]["rows"]}
    assert rows["as_built"]["system_median_m"] < rows["map_only"]["system_median_m"]
    # Калибровка сильнее всего помогает, когда длины известны только по карте
    assert rows["map_only"]["calibrated_median_m"] < rows["map_only"]["system_median_m"]


def test_e3_learning_curve(run):
    rows = run["results"]["e3"]["rows"]
    assert [r["incidents"] for r in rows] == [0, 10]
    assert rows[-1]["median_m"] < rows[0]["median_m"]


def test_e4_correlation(run):
    rows = {r["variant"]: r for r in run["results"]["e4"]["rows"]}
    assert rows["clean"]["hit_pct"] == 100.0 and rows["clean"]["cuts"] == CFG["cuts"]
    assert rows["missing"]["hit_pct"] <= 100.0


def test_e5_and_reports(run):
    row = run["results"]["e5"]["rows"][0]
    assert row["nodes"] > 50 and row["trace_ms"] > 0 and row["paths_cached_ms"] < row["paths_all_ms"]
    md = evaluation.to_markdown(run)
    for title in ("E1.", "E2.", "E3.", "E4.", "E5."):
        assert title in md
    csv = evaluation.to_csv(run, "e1").decode("utf-8-sig")
    assert csv.splitlines()[0].startswith("method;title;n;") and len(csv.splitlines()) == 5


def test_experiment_db_url():
    url = evaluation.experiment_db_url("postgresql+psycopg://u:p@h:5432/vols")
    assert url.endswith("/vols_experiment") and "u:p@" in url


def test_working_data_untouched(db):
    """Эксперимент идёт в отдельной базе: в рабочей сеть не меняется."""
    before = db.scalar(sa.text("SELECT count(*) FROM network_node"))
    evaluation.run_isolated(get_settings().database_url, "quick", only=["e5"], cfg={**CFG, "sizes": [80]})
    assert db.scalar(sa.text("SELECT count(*) FROM network_node")) == before


def test_api(client):
    from app.core.db import SessionLocal
    from app.core.security import hash_password
    from app.models import AppUser, Role
    pw = "Тест-пароль-12"
    with SessionLocal() as s:
        s.execute(sa.delete(AppUser).where(AppUser.login == "e_view"))
        s.add(AppUser(login="e_view", full_name="e", role=Role.viewer, password_hash=hash_password(pw)))
        s.commit()
    assert client.post("/api/auth/login", json={"login": "e_view", "password": pw}).status_code == 200
    assert client.get("/api/experiments").status_code == 403
    assert client.post("/api/experiments", headers={"X-Requested-With": "fetch"}, json={}).status_code == 403
