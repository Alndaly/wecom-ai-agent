import base64

import pytest

from app.ai import workflow
from app.models import Message


def test_image_message_prompt_text_preserves_media_context():
    msg = Message(type="image", content="[图片]")

    assert workflow._message_prompt_text(msg) == "客户发送了一张图片。[图片]"


def test_message_images_includes_image_typed_message(tmp_path, monkeypatch):
    img = tmp_path / "snap.jpg"
    payload = b"\xff\xd8\xff\xe0fake"
    img.write_bytes(payload)
    monkeypatch.setattr(workflow, "resolve_media_path", lambda meta: img)

    msg = Message(type="image", content="[图片]", media_json={"mime": "image/jpeg"})
    images = workflow._message_images([msg])

    assert images == [("image/jpeg", base64.b64encode(payload).decode("ascii"))]


def test_message_images_includes_text_typed_message_with_image_attachment(tmp_path, monkeypatch):
    img = tmp_path / "snap.png"
    payload = b"\x89PNG\r\n\x1a\nfake"
    img.write_bytes(payload)
    monkeypatch.setattr(workflow, "resolve_media_path", lambda meta: img)

    msg = Message(
        type="text",
        content="帮我看下这张图",
        media_json={"mime": "image/png"},
    )
    images = workflow._message_images([msg])

    assert images == [("image/png", base64.b64encode(payload).decode("ascii"))]


def test_message_images_skips_video_attachments(tmp_path, monkeypatch):
    vid = tmp_path / "clip.mp4"
    vid.write_bytes(b"fake-mp4")
    monkeypatch.setattr(workflow, "resolve_media_path", lambda meta: vid)

    msg = Message(type="video", content="[视频]", media_json={"mime": "video/mp4"})

    assert workflow._message_images([msg]) == []


def test_video_message_prompt_text_preserves_media_context():
    msg = Message(type="video", content="[视频]")

    assert workflow._message_prompt_text(msg) == "客户发送了一个视频。[视频]"


def test_text_message_with_media_prompt_text_preserves_attachment_context():
    msg = Message(type="text", content="帮我看下这张图", media_json={"mime": "image/png"})

    assert workflow._message_prompt_text(msg) == "客户发送了一张图片，并补充文字：帮我看下这张图"


def test_text_message_with_media_without_caption_uses_image_context_only():
    msg = Message(type="text", content="", media_json={"mime": "image/png"})

    assert workflow._message_prompt_text(msg) == "客户发送了一张图片。"
