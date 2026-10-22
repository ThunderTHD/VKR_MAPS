"""Действующие параметры калибровки — читаются трассировкой и локализацией.

Хранятся в последнем применённом (и не отменённом) прогоне калибровки:
- map_stretch — во сколько реальная трасса длиннее нарисованной (0,07 = на 7 %);
- geo_err_pct — погрешность длины «по карте» после поправки (2σ, %).
Укрутка и её погрешность хранятся у каждого кабеля (cable.twist_factor, twist_err_2s).
"""
from dataclasses import dataclass

import sqlalchemy as sa
from sqlalchemy.orm import Session

# Погрешность укрутки кабеля, для которого она не уточнена калибровкой (2σ, абсолютная).
# Типичный диапазон укрутки модульных кабелей 0,3–1,2 %; по умолчанию в базе 0,5 %.
TWIST_ERR_2S = 0.004


@dataclass(frozen=True)
class CalibState:
    map_stretch: float = 0.0
    geo_err_pct: float | None = None   # None — из справочника источников длин (25 %)
    run_id: int | None = None


RAW = CalibState()


def current_state(db: Session) -> CalibState:
    row = db.execute(sa.text("""
        SELECT id, params FROM calibration_run
        WHERE applied_at IS NOT NULL AND NOT coalesce((params->>'reverted')::boolean, false)
        ORDER BY applied_at DESC, id DESC LIMIT 1""")).first()
    if row is None:
        return RAW
    p = row.params or {}
    return CalibState(float(p.get("map_stretch", 0.0)), p.get("geo_err_pct"), row.id)
