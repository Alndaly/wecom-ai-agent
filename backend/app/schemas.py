from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, EmailStr, Field


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- auth ----------
class LoginIn(BaseModel):
    email: EmailStr
    password: str


class TokenOut(BaseModel):
    access_token: str
    refresh_token: str | None = None
    token_type: str = "bearer"


class RefreshTokenIn(BaseModel):
    refresh_token: str


class UserOut(ORM):
    id: int
    email: str
    display_name: str
    team_id: int


# ---------- robots ----------
class RobotCreateIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)


class RobotOut(ORM):
    id: int
    name: str
    robot_id: str
    status: str
    current_page: str | None = None
    device_type: str | None = None
    device_name: str | None = None
    manufacturer: str | None = None
    model: str | None = None
    android_version: str | None = None
    sdk_int: int | None = None
    app_version: str | None = None
    screen_width: int | None = None
    screen_height: int | None = None
    last_seen_at: datetime | None = None
    persona_id: str | None = None
    created_at: datetime


class RobotUpdateIn(BaseModel):
    # PATCH-style: every field is optional. The robots PATCH endpoint
    # treats `None` as "leave alone" and only applies fields actually
    # present in the body.
    name: str | None = Field(default=None, min_length=1, max_length=128)
    # Empty string means "clear the override and fall back to team default".
    persona_id: str | None = Field(default=None, max_length=64)


class RobotCreateOut(BaseModel):
    robot: RobotOut
    token: str  # one-time, plaintext


class RobotUiDumpRequestOut(BaseModel):
    request_id: str
    dispatched: bool


class RobotCommandOut(BaseModel):
    dispatched: bool


class AgentRunIn(BaseModel):
    goal: str
    max_steps: int = 8


class AgentRunOut(BaseModel):
    task_id: int
    accepted: bool


class RobotTaskLogOut(ORM):
    id: int
    robot_id: int
    task_id: int | None
    level: str
    message: str
    created_at: datetime


# ---------- contacts / conversations ----------
class ContactOut(ORM):
    id: int
    external_id: str
    nickname: str
    avatar: str | None = None
    stage: str
    tags_json: list[Any]


class ConversationOut(ORM):
    id: int
    robot_id: int
    contact_id: int
    mode: str
    operator_id: int | None = None
    unread_count: int
    last_message_at: datetime | None = None
    last_message_preview: str | None = None
    contact: ContactOut


class ConversationPatch(BaseModel):
    mode: Literal["ai", "human", "mixed"]


# ---------- messages ----------
class MessageOut(ORM):
    id: int
    conversation_id: int
    direction: str
    sender_type: str
    sender_id: int | None = None
    type: str
    content: str
    media_json: dict | None = None
    status: str | None = None
    feedback_status: str | None = None
    feedback_trace_id: str | None = None
    feedback_at: datetime | None = None
    feedback_reply_task_ids: list[int] | None = None
    external_msg_id: str | None = None
    task_id: int | None = None
    created_at: datetime


class MessageSendIn(BaseModel):
    type: Literal["text"] = "text"
    content: str = Field(min_length=1, max_length=4000)


class TaskOut(ORM):
    id: int
    robot_id: int
    type: str
    status: str
    attempts: int
    last_error: str | None = None


class MessageSendOut(BaseModel):
    message: MessageOut
    task: TaskOut


# ---------- android event payloads ----------
class AndroidContact(BaseModel):
    external_id: str
    nickname: str = ""
    avatar: str | None = None


class AndroidMessageReceived(BaseModel):
    contact: AndroidContact
    external_msg_id: str | None = None
    type: Literal["text", "image", "video"] = "text"
    content: str
    media_json: dict | None = None
    sender_type: Literal["customer", "human"] = "customer"
    sent_at: datetime | None = None
