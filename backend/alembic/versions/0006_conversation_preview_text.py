"""store full conversation preview text

Revision ID: 0006_preview_text
Revises: 0005_task_fk_set_null
Create Date: 2026-05-16
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_preview_text"
down_revision: Union[str, None] = "0005_task_fk_set_null"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.alter_column(
            "last_message_preview",
            existing_type=sa.String(length=512),
            type_=sa.Text(),
            existing_nullable=True,
        )


def downgrade() -> None:
    with op.batch_alter_table("conversations", schema=None) as batch_op:
        batch_op.alter_column(
            "last_message_preview",
            existing_type=sa.Text(),
            type_=sa.String(length=512),
            existing_nullable=True,
        )
