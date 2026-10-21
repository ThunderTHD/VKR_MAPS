"""Схема БД: миграции соответствуют моделям, справочники заполнены, правила целостности работают."""
import pytest
import sqlalchemy as sa
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy.exc import DBAPIError, IntegrityError

from app.core.db import engine
from app.core.migrations import include_object
from app.models import (
    Base,
    Cable,
    CableType,
    Closure,
    CrossPort,
    FiberEnd,
    LengthSource,
    NetworkNode,
    NodeKind,
    Operator,
    OpticalCross,
    Owner,
    RouteSegment,
    SegmentKind,
    Splice,
)
from app.services.network_ops import make_fibers


def test_migrations_match_models():
    """Если поменяли модель и забыли миграцию — тест упадёт."""
    with engine.connect() as conn:
        ctx = MigrationContext.configure(conn, opts={"include_object": include_object, "compare_type": True})
        diff = compare_metadata(ctx, Base.metadata)
    assert diff == [], f"Модели и миграции расходятся: {diff}"


def test_reference_data_seeded(db):
    sources = {s.code.value: s.default_error_pct for s in db.scalars(sa.select(LengthSource))}
    assert sources == {"reflectometer": 1.0, "as_built": 2.0, "passport": 5.0, "engineer": 10.0, "geometry": 25.0}
    assert {o.short_name for o in db.scalars(sa.select(Operator))} == {"Ростелеком", "Билайн", "МегаФон", "МТС"}
    assert db.scalar(sa.select(sa.func.count(Owner.id))) >= 5


def test_fiber_colors_follow_marking(db):
    """ДПО-П-08У (2х4): модуль 1 красный, модуль 2 натуральный; в модуле синий, оранжевый, зелёный, коричневый."""
    ctype = db.scalar(sa.select(CableType).where(CableType.mark.like("ДПО-П-08У%")))
    fibers = make_fibers(ctype)
    assert [f.number for f in fibers] == list(range(1, 9))
    assert [(f.module_no, f.number_in_module) for f in fibers][:5] == [(1, 1), (1, 2), (1, 3), (1, 4), (2, 1)]
    assert [f.color for f in fibers[:4]] == ["синий", "оранжевый", "зелёный", "коричневый"]
    assert fibers[0].module_color == "красный" and fibers[4].module_color == "натуральный"
    # Как в ИД Чапаева 62Б: модуль 1, волокно 3 — зелёное
    assert fibers[2].color == "зелёный"


def test_fiber_colors_96(db):
    ctype = db.scalar(sa.select(CableType).where(CableType.mark.like("ДПС-П-96У%")))
    fibers = make_fibers(ctype)
    assert len(fibers) == 96
    assert fibers[11].color == "бирюзовый" and fibers[12].color == "синий"  # 12-й цвет, затем по кругу


@pytest.fixture()
def two_cables(db):
    """Минимальная сеть: два кабеля сходятся в муфте."""
    op = db.scalar(sa.select(Operator))
    ctype = db.scalar(sa.select(CableType).where(CableType.mark.like("ОКЛ-8%")))
    a = NetworkNode(kind=NodeKind.well, code="к.1", geom="SRID=4326;POINT(52.0 55.0)")
    b = NetworkNode(kind=NodeKind.well, code="к.2", geom="SRID=4326;POINT(52.001 55.0)")
    db.add_all([a, b])
    db.flush()
    db.add(RouteSegment(kind=SegmentKind.duct, node_a_id=a.id, node_b_id=b.id, geom_length_m=64,
                        geom="SRID=4326;LINESTRING(52.0 55.0, 52.001 55.0)"))
    c1 = Cable(name="К1", operator_id=op.id, cable_type_id=ctype.id, fibers=make_fibers(ctype))
    c2 = Cable(name="К2", operator_id=op.id, cable_type_id=ctype.id, fibers=make_fibers(ctype))
    clo = Closure(node_id=b.id, number="М1")
    db.add_all([c1, c2, clo])
    db.flush()
    return c1, c2, clo


def test_splice_same_fiber_end_twice_rejected(db, two_cables):
    c1, c2, clo = two_cables
    db.add(Splice(closure_id=clo.id, fiber_a_id=c1.fibers[0].id, end_a=FiberEnd.b,
                  fiber_b_id=c2.fibers[0].id, end_b=FiberEnd.a))
    db.flush()
    # Тот же конец волокна К1-1 второй раз — база должна отказать
    with pytest.raises((IntegrityError, DBAPIError), match="уже подключён"), db.begin_nested():
        db.add(Splice(closure_id=clo.id, fiber_a_id=c1.fibers[0].id, end_a=FiberEnd.b,
                      fiber_b_id=c2.fibers[1].id, end_b=FiberEnd.a))
        db.flush()
    # Другой конец того же волокна (у начала кабеля) — можно
    db.add(Splice(closure_id=clo.id, fiber_a_id=c1.fibers[0].id, end_a=FiberEnd.a,
                  fiber_b_id=c2.fibers[2].id, end_b=FiberEnd.b))
    db.flush()


def test_cross_port_on_spliced_end_rejected(db, two_cables):
    c1, c2, clo = two_cables
    db.add(Splice(closure_id=clo.id, fiber_a_id=c1.fibers[0].id, end_a=FiberEnd.b,
                  fiber_b_id=c2.fibers[0].id, end_b=FiberEnd.a))
    odf = OpticalCross(node_id=clo.node_id, name="ODF-1", port_count=8)
    db.add(odf)
    db.flush()
    with pytest.raises((IntegrityError, DBAPIError), match="уже подключён"), db.begin_nested():
        db.add(CrossPort(cross_id=odf.id, port_no=1, fiber_id=c2.fibers[0].id, fiber_end=FiberEnd.a))
        db.flush()


def test_segment_fact_length_requires_source(db, two_cables):
    seg = db.scalar(sa.select(RouteSegment).order_by(RouteSegment.id.desc()))
    seg.fact_length_m = 70.0
    with pytest.raises(IntegrityError, match="length_has_source"), db.begin_nested():
        db.flush()
