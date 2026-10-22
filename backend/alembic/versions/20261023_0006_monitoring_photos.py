"""Мониторинг (время опроса, имитация аварий) и координаты фотографий

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-23
"""
from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from geoalchemy2 import Geometry

revision: str = "0006"
down_revision: str | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("access_node", sa.Column("last_poll_at", sa.DateTime(timezone=True), nullable=True))
    # Имитация аварий для режима simulate (ноутбук без доступа к узлам): обрыв участка или
    # отключение питания узла доступа; авто — созданные самим имитатором, иначе — вручную
    op.create_table(
        "sim_fault",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("kind", sa.String(length=10), nullable=False),
        sa.Column("segment_id", sa.Integer(), sa.ForeignKey("route_segment.id", ondelete="CASCADE"), nullable=True),
        sa.Column("access_node_id", sa.Integer(), sa.ForeignKey("access_node.id", ondelete="CASCADE"),
                  nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("auto", sa.Boolean(), server_default=sa.false(), nullable=False),
        sa.Column("note", sa.String(length=200), nullable=True),
        sa.CheckConstraint("kind IN ('cut', 'power')", name=op.f("ck_sim_fault_kind")),
        sa.CheckConstraint("(kind = 'cut' AND segment_id IS NOT NULL) "
                           "OR (kind = 'power' AND access_node_id IS NOT NULL)", name=op.f("ck_sim_fault_target")),
    )
    # Где снято фото: из EXIF или распознано с подписи на кадре
    op.add_column("document", sa.Column("geom", Geometry(geometry_type="POINT", srid=4326, spatial_index=False),
                                        nullable=True))
    op.add_column("document", sa.Column("taken_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("document", sa.Column("coord_source", sa.String(length=10), nullable=True))
    op.create_geospatial_index("idx_document_geom", "document", ["geom"], unique=False, postgresql_using="gist",
                               postgresql_ops={})


def downgrade() -> None:
    op.drop_geospatial_index("idx_document_geom", table_name="document", postgresql_using="gist",
                             column_name="geom")
    op.drop_column("document", "coord_source")
    op.drop_column("document", "taken_at")
    op.drop_column("document", "geom")
    op.drop_table("sim_fault")
    op.drop_column("access_node", "last_poll_at")
