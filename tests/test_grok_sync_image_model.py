# encoding:utf-8

from pathlib import Path

from bridge.context import Context, ContextType
from bridge.reply import ReplyType
from models.grok import grok_image


def test_sync_model_extracts_first_image_reference_for_image_to_image(tmp_path):
    image_path = tmp_path / "out.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    calls = []

    class FakeProvider:
        last_prompt_metadata = None

        def generate(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return str(image_path)

    context = Context(ContextType.IMAGE_CREATE, "参考这张图画海报\n[图片: C:/tmp/ref.png]")
    reply = grok_image.generate_reply(context.content, context, provider=FakeProvider())

    assert reply.type == ReplyType.IMAGE
    assert reply.content == str(image_path)
    assert calls[0][0] == "参考这张图画海报"
    assert calls[0][1]["image_url"] == "C:/tmp/ref.png"
    assert calls[0][1]["model"] == "grok-imagine-image"
    assert calls[0][1]["prompt_enhancement"] is True


def test_sync_model_returns_clear_error_when_edit_intent_has_no_image():
    reply = grok_image.generate_reply("参考上图换背景")

    assert reply.type == ReplyType.ERROR
    assert "请先上传一张图片" in reply.content


def test_sync_model_text_to_image_regression(tmp_path):
    image_path = tmp_path / "out.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    calls = []

    class FakeProvider:
        last_prompt_metadata = None

        def generate(self, prompt, **kwargs):
            calls.append((prompt, kwargs))
            return str(image_path)

    reply = grok_image.generate_reply("draw a red kite", provider=FakeProvider())

    assert reply.type == ReplyType.IMAGE
    assert calls[0][0] == "draw a red kite"
    assert calls[0][1]["image_url"] is None
    assert calls[0][1]["aspect_ratio"] is None
    assert calls[0][1]["resolution"] is None
    assert calls[0][1]["prompt_enhancement"] is True


def test_sync_model_uses_prompt_reference_dimensions_when_available(tmp_path):
    reference = tmp_path / "ref.png"
    reference.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + b"\x00\x00\x00\rIHDR"
        + (900).to_bytes(4, "big")
        + (1600).to_bytes(4, "big")
    )
    output = tmp_path / "out.png"
    output.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    calls = []

    class FakeProvider:
        last_prompt_metadata = None

        def generate(self, prompt, **kwargs):
            calls.append(kwargs)
            return str(output)

    reply = grok_image.generate_reply(f"图生图，改成电影海报\n[图片: {reference}]", provider=FakeProvider())

    assert reply.type == ReplyType.IMAGE
    assert calls[0]["image_url"] == str(reference)
    assert calls[0]["aspect_ratio"] == "9:16"
    assert calls[0]["resolution"] == "2k"
