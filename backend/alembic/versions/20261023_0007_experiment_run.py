"""Прогоны эксперимента для ВКР: параметры, ход расчёта, результаты

Revision ID: 0007
Revises: 0006
Create Date: 2026-10-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision: str = "0007"
down_revision: str | None = "0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "experiment_run",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("suite", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=12), nullable=False),
        sa.Column("progress", JSONB(), nullable=True),
        sa.Column("results", JSONB(), nullable=True),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("started_by", sa.Integer(), sa.ForeignKey("app_user.id", ondelete="SET NULL"), nullable=True),
        sa.CheckConstraint("status IN ('running', 'done', 'failed')", name=op.f("ck_experiment_run_status")),
    )


def downgrade() -> None:
    op.drop_table("experiment_run")
