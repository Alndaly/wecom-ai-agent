"""Add Robot.persona_id (per-device persona override).

Revision ID: 0010_robot_persona_id
Revises: 0009_task_queue_priority
Create Date: 2026-05-18
"""
from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_robot_persona_id"
down_revision = "0009_task_queue_priority"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "robots",
        sa.Column("persona_id", sa.String(length=64), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("robots", "persona_id")
