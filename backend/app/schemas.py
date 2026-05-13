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
    token_type: str = "bearer"


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
    last_seen_at: datetime | None = None
    created_at: datetime


class RobotCreateOut(BaseModel):
    robot: RobotOut
    token: str  # one-time, plaintext


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
    status: str | None = None
    external_msg_id: str | None = None
    task_id: int | None = None
    created_at: datetime


class MessageSendIn(BaseModel):
    type: Literal["text"] = "text"  # MVP1: text only
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
    type: Literal["text"] = "text"
    content: str
    sent_at: datetime | None = None
