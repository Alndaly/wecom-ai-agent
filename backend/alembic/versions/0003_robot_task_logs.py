"""add robot task logs

Revision ID: 0003_robot_task_logs
Revises: 0002_robot_device_info
Create Date: 2026-05-15
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0003_robot_task_logs"
down_revision: Union[str, None] = "0002_robot_device_info"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "robot_task_logs",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("robot_id", sa.Integer(), sa.ForeignKey("robots.id"), nullable=False),
        sa.Column("task_id", sa.Integer(), sa.ForeignKey("robot_tasks.id"), nullable=True),
        sa.Column("level", sa.String(length=16), nullable=False, server_default="info"),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_task_log_task_created", "robot_task_logs", ["task_id", "created_at"])
    op.create_index(op.f("ix_robot_task_logs_robot_id"), "robot_task_logs", ["robot_id"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_robot_task_logs_robot_id"), table_name="robot_task_logs")
    op.drop_index("ix_task_log_task_created", table_name="robot_task_logs")
    op.drop_table("robot_task_logs")
