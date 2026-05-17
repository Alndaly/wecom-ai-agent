"""Add persisted task queue ordering fields.

Revision ID: 0009_task_queue_priority
Revises: 0008_message_media_json
Create Date: 2026-05-17
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_task_queue_priority"
down_revision = "0008_message_media_json"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "robot_tasks",
        sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
    )
    op.add_column("robot_tasks", sa.Column("queue_seq", sa.Integer(), nullable=True))
    op.create_index("ix_robot_tasks_priority", "robot_tasks", ["priority"])
    op.create_index("ix_robot_tasks_queue_seq", "robot_tasks", ["queue_seq"])
    op.alter_column("robot_tasks", "priority", server_default=None)


def downgrade() -> None:
    op.drop_index("ix_robot_tasks_queue_seq", table_name="robot_tasks")
    op.drop_index("ix_robot_tasks_priority", table_name="robot_tasks")
    op.drop_column("robot_tasks", "queue_seq")
    op.drop_column("robot_tasks", "priority")
