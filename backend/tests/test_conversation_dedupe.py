from __future__ import annotations

from datetime import timedelta
import pytest

from app.models import Message, utcnow
from app.services import conversation


@pytest.mark.asyncio
async def test_recent_inbound_same_content_is_duplicate(fake_db):
    fake_db.messages.append(
        Message(
            conversation_id=123,
            direction="in",
            sender_type="customer",
            type="text",
            content="你好",
            external_msg_id="notif:1000:abc",
            created_at=utcnow() - timedelta(seconds=30),
        )
    )

    assert await conversation._has_recent_inbound_same_content(
        fake_db, 123, "你好", utcnow(), "a11y:2000:def"
    )


@pytest.mark.asyncio
async def test_recent_inbound_same_source_same_content_is_not_duplicate(fake_db):
    fake_db.messages.append(
        Message(
            conversation_id=123,
            direction="in",
            sender_type="customer",
            type="text",
            content="你好",
            external_msg_id="a11y:1000:abc",
            created_at=utcnow() - timedelta(seconds=30),
        )
    )

    assert not await conversation._has_recent_inbound_same_content(
        fake_db, 123, "你好", utcnow(), "a11y:2000:def"
    )


@pytest.mark.asyncio
async def test_old_inbound_same_content_is_not_duplicate(fake_db):
    fake_db.messages.append(
        Message(
            conversation_id=123,
            direction="in",
            sender_type="customer",
            type="text",
            content="你好",
            external_msg_id="notif:1000:abc",
            created_at=utcnow() - timedelta(minutes=5),
        )
    )

    assert not await conversation._has_recent_inbound_same_content(
        fake_db, 123, "你好", utcnow(), "a11y:2000:def"
    )


@pytest.fixture
def fake_db():
    class FakeDb:
        def __init__(self):
            self.messages: list[Message] = []

        async def execute(self, stmt):
            for msg in self.messages:
                ok = (
                    msg.conversation_id == 123
                    and msg.direction == "in"
                    and msg.sender_type == "customer"
                    and msg.content == "你好"
                    and msg.created_at > utcnow() - timedelta(seconds=90)
                )
                if ok:
                    return FakeResult([msg.external_msg_id])
            return FakeResult([])

    class FakeResult:
        def __init__(self, rows):
            self.rows = rows

        def scalars(self):
            return self

        def all(self):
            return self.rows

    return FakeDb()
