"""Поля для калибровки: погрешность и происхождение коэффициента укрутки кабеля

После калибровки у кабеля известна не только укрутка, но и её погрешность (2σ) —
локализация использует её вместо значения по умолчанию.

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0004"
down_revision: str | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("cable", sa.Column("twist_err_2s", sa.Float(), nullable=True))
    op.add_column("cable", sa.Column("twist_source", sa.String(length=20), nullable=True))


def downgrade() -> None:
    op.drop_column("cable", "twist_source")
    op.drop_column("cable", "twist_err_2s")
