"""track per-message feedback status

Revision ID: 0004_message_feedback_tracking
Revises: 0003_robot_task_logs
Create Date: 2026-05-16
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0004_message_feedback_tracking"
down_revision: Union[str, None] = "0003_robot_task_logs"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.add_column(sa.Column("feedback_status", sa.String(length=16), nullable=True))
        batch_op.add_column(sa.Column("feedback_trace_id", sa.String(length=64), nullable=True))
        batch_op.add_column(sa.Column("feedback_at", sa.DateTime(timezone=True), nullable=True))
        batch_op.add_column(sa.Column("feedback_reply_task_ids", sa.JSON(), nullable=True))
        batch_op.create_index("ix_msg_feedback_pending", ["conversation_id", "feedback_status"])


def downgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_index("ix_msg_feedback_pending")
        batch_op.drop_column("feedback_reply_task_ids")
        batch_op.drop_column("feedback_at")
        batch_op.drop_column("feedback_trace_id")
        batch_op.drop_column("feedback_status")
