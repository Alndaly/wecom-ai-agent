from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from sqlalchemy import (
    JSON,
    BigInteger,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.db import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Team(Base):
    __tablename__ = "teams"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"))
    email: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Robot(Base):
    __tablename__ = "robots"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    robot_id: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    token_hash: Mapped[str] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(16), default="offline")  # offline/online/busy
    current_page: Mapped[str | None] = mapped_column(String(32), nullable=True)
    device_type: Mapped[str | None] = mapped_column(String(32), nullable=True)
    device_name: Mapped[str | None] = mapped_column(String(128), nullable=True)
    manufacturer: Mapped[str | None] = mapped_column(String(64), nullable=True)
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)
    android_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    sdk_int: Mapped[int | None] = mapped_column(Integer, nullable=True)
    app_version: Mapped[str | None] = mapped_column(String(32), nullable=True)
    screen_width: Mapped[int | None] = mapped_column(Integer, nullable=True)
    screen_height: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Contact(Base):
    __tablename__ = "contacts"
    __table_args__ = (UniqueConstraint("robot_id", "external_id", name="uq_contact_robot_ext"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    robot_id: Mapped[int] = mapped_column(ForeignKey("robots.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    nickname: Mapped[str] = mapped_column(String(255), default="")
    avatar: Mapped[str | None] = mapped_column(Text, nullable=True)
    stage: Mapped[str] = mapped_column(String(32), default="new")
    tags_json: Mapped[list[Any]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class Conversation(Base):
    __tablename__ = "conversations"
    __table_args__ = (UniqueConstraint("robot_id", "contact_id", name="uq_conv_robot_contact"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    robot_id: Mapped[int] = mapped_column(ForeignKey("robots.id"), index=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    mode: Mapped[str] = mapped_column(String(16), default="mixed")  # ai/human/mixed
    operator_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), nullable=True)
    unread_count: Mapped[int] = mapped_column(Integer, default=0)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_message_preview: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)

    contact: Mapped[Contact] = relationship(lazy="joined")


class Message(Base):
    __tablename__ = "messages"
    __table_args__ = (
        Index("ix_msg_conv_created", "conversation_id", "created_at"),
        UniqueConstraint("conversation_id", "external_msg_id", name="uq_msg_conv_external"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"))
    direction: Mapped[str] = mapped_column(String(4))  # in / out
    sender_type: Mapped[str] = mapped_column(String(16))  # customer/ai/human/system
    sender_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    type: Mapped[str] = mapped_column(String(16), default="text")
    content: Mapped[str] = mapped_column(Text)
    media_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str | None] = mapped_column(String(16), nullable=True)  # for out only
    feedback_status: Mapped[str | None] = mapped_column(String(16), nullable=True)
    feedback_trace_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    feedback_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    feedback_reply_task_ids: Mapped[list[int] | None] = mapped_column(JSON, nullable=True)
    external_msg_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("robot_tasks.id", ondelete="SET NULL"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RobotTask(Base):
    __tablename__ = "robot_tasks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    robot_id: Mapped[int] = mapped_column(ForeignKey("robots.id"), index=True)
    type: Mapped[str] = mapped_column(String(32))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    status: Mapped[str] = mapped_column(String(16), default="pending", index=True)
    # pending/dispatched/running/completed/failed/timeout
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=2)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    conversation_id: Mapped[int | None] = mapped_column(ForeignKey("conversations.id"), nullable=True)
    message_id: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class RobotTaskLog(Base):
    __tablename__ = "robot_task_logs"
    __table_args__ = (Index("ix_task_log_task_created", "task_id", "created_at"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    robot_id: Mapped[int] = mapped_column(ForeignKey("robots.id"), index=True)
    task_id: Mapped[int | None] = mapped_column(
        ForeignKey("robot_tasks.id", ondelete="SET NULL"), nullable=True, index=True
    )
    level: Mapped[str] = mapped_column(String(16), default="info")
    message: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AIPrompt(Base):
    __tablename__ = "ai_prompts"
    __table_args__ = (UniqueConstraint("team_id", "key", name="uq_ai_prompt_team_key"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))  # e.g. "default", "welcome", "complaint"
    content: Mapped[str] = mapped_column(Text)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class AIReplyLog(Base):
    __tablename__ = "ai_reply_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    conversation_id: Mapped[int] = mapped_column(ForeignKey("conversations.id"), index=True)
    message_id: Mapped[int | None] = mapped_column(ForeignKey("messages.id"), nullable=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(16))  # reply / suggest / skip
    text: Mapped[str | None] = mapped_column(Text, nullable=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    model: Mapped[str] = mapped_column(String(64), default="")
    latency_ms: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(255), default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# MVP3: knowledge base + long-term memory
# ---------------------------------------------------------------------------
class KnowledgeBase(Base):
    __tablename__ = "knowledge_bases"
    __table_args__ = (UniqueConstraint("team_id", "name", name="uq_kb_team_name"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    name: Mapped[str] = mapped_column(String(128))
    description: Mapped[str] = mapped_column(Text, default="")
    chunk_size: Mapped[int] = mapped_column(Integer, default=400)
    chunk_overlap: Mapped[int] = mapped_column(Integer, default=60)
    version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class KnowledgeDocument(Base):
    __tablename__ = "knowledge_documents"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    name: Mapped[str] = mapped_column(String(255))
    source: Mapped[str] = mapped_column(String(32), default="upload")  # upload / url / paste
    mime: Mapped[str] = mapped_column(String(64), default="text/plain")
    status: Mapped[str] = mapped_column(String(16), default="pending")
    # pending / processing / ready / failed
    chunk_count: Mapped[int] = mapped_column(Integer, default=0)
    bytes: Mapped[int] = mapped_column(Integer, default=0)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class KnowledgeChunk(Base):
    __tablename__ = "knowledge_chunks"
    __table_args__ = (Index("ix_chunk_doc_ord", "doc_id", "ord"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    kb_id: Mapped[int] = mapped_column(ForeignKey("knowledge_bases.id"), index=True)
    doc_id: Mapped[int] = mapped_column(ForeignKey("knowledge_documents.id"), index=True)
    ord: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    embedding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    # for memory store: in-memory backend just keeps the vector inline as JSON
    embedding_json: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    entities_json: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserProfile(Base):
    """Long-term, structured memory keyed by contact."""
    __tablename__ = "user_profiles"

    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), primary_key=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    preferences_json: Mapped[dict] = mapped_column(JSON, default=dict)
    stage: Mapped[str] = mapped_column(String(32), default="new")
    last_summary_message_id: Mapped[int | None] = mapped_column(Integer, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class UserMemory(Base):
    """Semantic memory entries (event-like). Vector kept inline for in-memory backend."""
    __tablename__ = "user_memories"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    contact_id: Mapped[int] = mapped_column(ForeignKey("contacts.id"), index=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    kind: Mapped[str] = mapped_column(String(32), default="summary")
    # summary / preference / event / intent
    content: Mapped[str] = mapped_column(Text)
    embedding_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_json: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


# ---------------------------------------------------------------------------
# Runtime, team-scoped configuration (LLM, embedding, retrieval, ...)
# ---------------------------------------------------------------------------
class TeamSetting(Base):
    """One row per (team_id, key). Value is a JSON blob.

    `key` taxonomy:
      - "llm"        → provider, model, api_key, base_url, temperature
      - "embedding"  → provider, model, api_key, base_url, dim
      - "retrieval"  → top_k, min_score
      - "ai"         → confidence_threshold, context_window, default_prompt
    """
    __tablename__ = "team_settings"
    __table_args__ = (UniqueConstraint("team_id", "key", name="uq_team_setting"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    team_id: Mapped[int] = mapped_column(ForeignKey("teams.id"), index=True)
    key: Mapped[str] = mapped_column(String(64))
    value_json: Mapped[dict] = mapped_column(JSON, default=dict)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
