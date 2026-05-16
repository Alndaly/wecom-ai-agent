"""set task references null when robot tasks are deleted

Revision ID: 0005_task_fk_set_null
Revises: 0004_message_feedback_tracking
Create Date: 2026-05-16
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0005_task_fk_set_null"
down_revision: Union[str, None] = "0004_message_feedback_tracking"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def _fk_has_set_null(table_name: str, constraint_name: str) -> bool:
    bind = op.get_bind()
    row = bind.execute(
        sa.text(
            """
        SELECT rc.delete_rule
        FROM information_schema.referential_constraints rc
        JOIN information_schema.table_constraints tc
          ON tc.constraint_catalog = rc.constraint_catalog
         AND tc.constraint_schema = rc.constraint_schema
         AND tc.constraint_name = rc.constraint_name
        WHERE tc.table_name = :table_name
          AND tc.constraint_name = :constraint_name
        """
        ),
        {"table_name": table_name, "constraint_name": constraint_name},
    ).first()
    return bool(row and row[0] == "SET NULL")


def upgrade() -> None:
    if not _fk_has_set_null("messages", "messages_task_id_fkey"):
        op.drop_constraint("messages_task_id_fkey", "messages", type_="foreignkey")
        op.create_foreign_key(
            "messages_task_id_fkey",
            "messages",
            "robot_tasks",
            ["task_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if not _fk_has_set_null("robot_task_logs", "robot_task_logs_task_id_fkey"):
        op.drop_constraint("robot_task_logs_task_id_fkey", "robot_task_logs", type_="foreignkey")
        op.create_foreign_key(
            "robot_task_logs_task_id_fkey",
            "robot_task_logs",
            "robot_tasks",
            ["task_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    op.drop_constraint("robot_task_logs_task_id_fkey", "robot_task_logs", type_="foreignkey")
    op.create_foreign_key(
        "robot_task_logs_task_id_fkey",
        "robot_task_logs",
        "robot_tasks",
        ["task_id"],
        ["id"],
    )
    op.drop_constraint("messages_task_id_fkey", "messages", type_="foreignkey")
    op.create_foreign_key(
        "messages_task_id_fkey",
        "messages",
        "robot_tasks",
        ["task_id"],
        ["id"],
    )
