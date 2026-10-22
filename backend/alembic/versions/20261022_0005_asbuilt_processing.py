"""Пакеты ИД: фоновая обработка (статусы processing/failed), имя файла, ошибка, связь с кабелем

Revision ID: 0005
Revises: 0004
Create Date: 2026-10-22
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005"
down_revision: str | None = "0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

OLD = "'parsed', 'confirmed', 'rejected'"
NEW = "'processing', 'failed', 'parsed', 'confirmed', 'rejected'"


def upgrade() -> None:
    op.drop_constraint(op.f("ck_asbuilt_package_package_status"), "asbuilt_package", type_="check")
    op.create_check_constraint(op.f("ck_asbuilt_package_package_status"), "asbuilt_package", f"status IN ({NEW})")
    op.add_column("asbuilt_package", sa.Column("filename", sa.String(length=255), nullable=True))
    op.add_column("asbuilt_package", sa.Column("error", sa.Text(), nullable=True))
    op.add_column("asbuilt_package", sa.Column("cable_id", sa.Integer(), nullable=True))
    op.create_foreign_key(op.f("fk_asbuilt_package_cable_id_cable"), "asbuilt_package", "cable", ["cable_id"], ["id"],
                          ondelete="SET NULL")


def downgrade() -> None:
    op.drop_constraint(op.f("fk_asbuilt_package_cable_id_cable"), "asbuilt_package", type_="foreignkey")
    op.drop_column("asbuilt_package", "cable_id")
    op.drop_column("asbuilt_package", "error")
    op.drop_column("asbuilt_package", "filename")
    op.drop_constraint(op.f("ck_asbuilt_package_package_status"), "asbuilt_package", type_="check")
    op.create_check_constraint(op.f("ck_asbuilt_package_package_status"), "asbuilt_package", f"status IN ({OLD})")
