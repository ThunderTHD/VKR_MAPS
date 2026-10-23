"""Системы координат (МСК и др.) для импорта/экспорта и показа координат

Revision ID: 0008
Revises: 0007
Create Date: 2026-10-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0008"
down_revision: str | None = "0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "crs_def",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("name", sa.String(length=120), nullable=False, unique=True),
        # Определение в любом виде, который понимает PROJ: WKT из .prj, строка proj, «EPSG:32639»
        sa.Column("definition", sa.Text(), nullable=False),
        sa.Column("show_in_cards", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.add_column("import_job", sa.Column("crs_id", sa.Integer(), nullable=True))
    op.create_foreign_key(op.f("fk_import_job_crs_id_crs_def"), "import_job", "crs_def", ["crs_id"], ["id"],
                          ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint(op.f("fk_import_job_crs_id_crs_def"), "import_job", type_="foreignkey")
    op.drop_column("import_job", "crs_id")
    op.drop_table("crs_def")
