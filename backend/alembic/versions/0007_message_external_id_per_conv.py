"""scope message external_msg_id uniqueness to (conversation, external_msg_id)

Same external_msg_id hash can legitimately be emitted by two different devices
(the Android-side hash doesn't include device id). The previous global unique
constraint caused the second device's inbound message to collide and get
dropped/raised. Scope the uniqueness to the conversation so each robot's
ingestion is isolated.

Revision ID: 0007_msg_ext_per_conv
Revises: 0006_preview_text
Create Date: 2026-05-16
"""
from __future__ import annotations

from typing import Sequence, Union

from alembic import op


revision: str = "0007_msg_ext_per_conv"
down_revision: Union[str, None] = "0006_preview_text"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_constraint("uq_msg_external", type_="unique")
        batch_op.create_unique_constraint(
            "uq_msg_conv_external", ["conversation_id", "external_msg_id"]
        )


def downgrade() -> None:
    with op.batch_alter_table("messages", schema=None) as batch_op:
        batch_op.drop_constraint("uq_msg_conv_external", type_="unique")
        batch_op.create_unique_constraint("uq_msg_external", ["external_msg_id"])
