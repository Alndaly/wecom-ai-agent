"""add robot device info fields

Revision ID: 0002_robot_device_info
Revises: 0001_initial
Create Date: 2026-05-14
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0002_robot_device_info"
down_revision: Union[str, None] = "0001_initial"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("robots", schema=None) as batch_op:
        batch_op.add_column(sa.Column("device_type", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("device_name", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("manufacturer", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("model", sa.String(length=128), nullable=True))
        batch_op.add_column(sa.Column("android_version", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("sdk_int", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("app_version", sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column("screen_width", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("screen_height", sa.Integer(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("robots", schema=None) as batch_op:
        batch_op.drop_column("screen_height")
        batch_op.drop_column("screen_width")
        batch_op.drop_column("app_version")
        batch_op.drop_column("sdk_int")
        batch_op.drop_column("android_version")
        batch_op.drop_column("model")
        batch_op.drop_column("manufacturer")
        batch_op.drop_column("device_name")
        batch_op.drop_column("device_type")
