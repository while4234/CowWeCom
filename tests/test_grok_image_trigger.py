# encoding:utf-8

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bridge.bridge import Bridge
from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.channel import Channel
from channel.chat_channel import ChatChannel
from channel.image_recognition import ImageRecognitionManager, reset_image_recognition_manager
from common.image_prompt_enhancer import load_prompt_history
from channel.wecom_bot.wecom_bot_channel import WecomBotChannel
from integrations.hermes_xai.media_download import new_generated_media_path
from models.grok.grok_bot import GrokBot


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def test_grok_bot_image_create_returns_local_image_reply(monkeypatch, tmp_path):
    image_path = tmp_path / "grok.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    class FakeProvider:
        def generate(self, prompt, **kwargs):
            assert prompt == "draw a red kite"
            assert kwargs["image_url"] is None
            return str(image_path)

    monkeypatch.setattr("models.grok.grok_image.XAIImageGenProvider", lambda: FakeProvider())

    context = Context(ContextType.IMAGE_CREATE, "draw a red kite")
    reply = object.__new__(GrokBot).reply(context.content, context)

    assert reply.type == ReplyType.IMAGE_URL
    assert reply.content == f"file://{image_path}"
    assert not reply.content.startswith(("http://", "https://"))
    assert reply.cleanup_after_send is True
    assert reply.generated_media_path == str(image_path)
    assert context["cleanup_after_send"] is True
    assert context["generated_media_paths"] == [str(image_path)]


def test_grok_image_records_prompt_history_without_provider_metadata(monkeypatch, tmp_path):
    image_path = tmp_path / "grok.png"
    image_path.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    class FakeProvider:
        last_prompt_metadata = None

        def generate(self, prompt, **_kwargs):
            assert prompt == "draw a quiet lake"
            return str(image_path)

    monkeypatch.setattr("models.grok.grok_image.XAIImageGenProvider", lambda: FakeProvider())
    monkeypatch.setattr("models.grok.grok_image.conf", lambda: SimpleNamespace(get=lambda key, default=None: str(tmp_path / "cow") if key == "agent_workspace" else default))

    context = Context(ContextType.IMAGE_CREATE, "draw a quiet lake")
    context["memory_user_id"] = "user_a"
    context["session_id"] = "session_a"
    reply = object.__new__(GrokBot).reply(context.content, context)

    records = load_prompt_history(
        workspace_root=str(tmp_path / "cow"),
        memory_user_id="user_a",
        session_id="session_a",
        limit=1,
    )
    assert reply.type == ReplyType.IMAGE_URL
    assert records[0]["enhanced_prompt"] == "draw a quiet lake"
    assert records[0]["original_prompt"] == "draw a quiet lake"
    assert records[0]["enhanced"] is False
    assert records[0]["disabled_reason"] == "provider_metadata_missing"


def test_default_agent_mode_image_create_shortcuts_to_grok_image(monkeypatch):
    context = Context(ContextType.IMAGE_CREATE, "draw a cat")
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "agent": True,
        "text_to_image": "xai",
    }.get(key, default)
    called = []

    class TestChannel(Channel):
        channel_type = "wecom_bot"

    def fake_fetch(query, ctx):
        called.append((query, ctx))
        return "image-reply"

    monkeypatch.setattr("channel.channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.channel.Bridge", lambda: SimpleNamespace(fetch_reply_content=fake_fetch))

    assert TestChannel().build_reply_content("draw a cat", context) == "image-reply"
    assert called == [("draw a cat", context)]


def test_active_grok_profile_image_create_shortcuts_without_global_provider(monkeypatch):
    context = Context(ContextType.IMAGE_CREATE, "draw a cat")
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "agent": True,
        "text_to_image": "dall-e-2",
    }.get(key, default)
    called = []

    class TestChannel(Channel):
        channel_type = "wecom_bot"

    def fake_fetch(query, ctx):
        called.append((query, ctx))
        return "grok-image-reply"

    monkeypatch.setattr("channel.channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.channel.active_backend_is_grok_for_context", lambda ctx: True)
    monkeypatch.setattr("models.grok.grok_image.is_grok_image_provider", lambda: False)
    monkeypatch.setattr("channel.channel.Bridge", lambda: SimpleNamespace(fetch_reply_content=fake_fetch))

    assert TestChannel().build_reply_content("draw a cat", context) == "grok-image-reply"
    assert called == [("draw a cat", context)]


def test_non_grok_image_create_keeps_agent_mode(monkeypatch):
    context = Context(ContextType.IMAGE_CREATE, "draw normally")
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "agent": True,
        "text_to_image": "dall-e-2",
    }.get(key, default)
    called = []

    class TestChannel(Channel):
        channel_type = "web"

    def fake_agent(query, context, on_event=None, clear_history=False):
        called.append((query, context, clear_history))
        return "agent-reply"

    monkeypatch.setattr("channel.channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.channel.Bridge", lambda: SimpleNamespace(fetch_agent_reply=fake_agent))

    assert TestChannel().build_reply_content("draw normally", context) == "agent-reply"
    assert called == [("draw normally", context, False)]


def test_active_grok_profile_image_create_routes_to_grok_without_text_to_image_flag(monkeypatch):
    bridge_cls = _singleton_class(Bridge)
    bridge = object.__new__(bridge_cls)
    context = Context(ContextType.IMAGE_CREATE, "draw a cat")
    context["_actor_profile"] = SimpleNamespace(actor_id="admin", is_admin=True)
    calls = []

    def fake_grok_reply(prompt, ctx):
        calls.append((prompt, ctx))
        return Reply(ReplyType.IMAGE, "grok.png")

    monkeypatch.setattr("bridge.bridge.get_current_backend_for_profile", lambda profile: "grok")
    monkeypatch.setattr("models.grok.grok_image.is_grok_image_provider", lambda: False)
    monkeypatch.setattr("models.grok.grok_image.generate_reply", fake_grok_reply)

    reply = bridge.fetch_reply_content("draw a cat", context)

    assert reply.type == ReplyType.IMAGE
    assert reply.content == "grok.png"
    assert calls == [("draw a cat", context)]


def test_active_grok_profile_image_create_explicit_gpt_bypasses_grok(monkeypatch):
    bridge_cls = _singleton_class(Bridge)
    bridge = object.__new__(bridge_cls)
    context = Context(ContextType.IMAGE_CREATE, "Use GPT to draw a cat")
    context["_actor_profile"] = SimpleNamespace(actor_id="admin", is_admin=True)
    grok_calls = []

    class FakeChatBot:
        def reply(self, query, ctx):
            return Reply(ReplyType.IMAGE_URL, "gpt-image-url")

    monkeypatch.setattr("bridge.bridge.get_current_backend_for_profile", lambda profile: "grok")
    monkeypatch.setattr("common.llm_backend_router.get_current_backend", lambda: "capi")
    monkeypatch.setattr("common.llm_backend_quota_refresh.note_user_visible_model_call", lambda *args, **kwargs: None)
    monkeypatch.setattr("common.capi_monthly_monitor.maybe_check_capi_monthly_after_task", lambda *args, **kwargs: None)
    monkeypatch.setattr("models.grok.grok_image.is_grok_image_provider", lambda: False)
    monkeypatch.setattr("models.grok.grok_image.generate_reply", lambda *args, **kwargs: grok_calls.append(args))
    monkeypatch.setattr(bridge, "get_bot", lambda typename: FakeChatBot())

    reply = bridge.fetch_reply_content("Use GPT to draw a cat", context)

    assert reply.type == ReplyType.IMAGE_URL
    assert reply.content == "gpt-image-url"
    assert grok_calls == []


def _chat_msg():
    return SimpleNamespace(
        from_user_id="session",
        from_user_nickname="User",
        other_user_id="session",
        other_user_nickname="User",
        to_user_id="bot",
        actual_user_id="session",
        actual_user_nickname="User",
        is_at=False,
        at_list=[],
        self_display_name="bot",
    )


def _patch_image_create_conf(monkeypatch):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "single_chat_prefix": [""],
        "group_chat_prefix": [],
        "image_create_prefix": [],
        "video_create_prefix": [],
        "background_image_recognition_enabled": True,
        "image_recognition_image_create_auto_ref_window_seconds": 600,
    }.get(key, default)
    fake_conf.get_user_data.return_value = {}
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)


def test_grok_image_reference_request_promotes_to_image_create(monkeypatch):
    _patch_image_create_conf(monkeypatch)
    monkeypatch.setattr("channel.chat_channel.active_backend_is_grok_for_context", lambda ctx: True)

    channel = object.__new__(ChatChannel)
    channel.channel_type = "web"
    channel.user_id = "bot"
    channel.name = "bot"

    context = ChatChannel._compose_context(
        channel,
        ContextType.TEXT,
        "edit this image into a poster",
        msg=_chat_msg(),
        isgroup=False,
    )

    assert context.type == ContextType.IMAGE_CREATE
    assert context.content == "edit this image into a poster"


def test_grok_image_reference_request_attaches_recent_image(monkeypatch, tmp_path):
    _patch_image_create_conf(monkeypatch)
    monkeypatch.setattr("channel.chat_channel.active_backend_is_grok_for_context", lambda ctx: True)

    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = ImageRecognitionManager(workspace_root=str(tmp_path / "workspace"), max_workers=1)
    reset_image_recognition_manager(manager)
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        record = manager.register_image(
            session_id="session",
            channel_type="web",
            image_path=str(source),
        )

    channel = object.__new__(ChatChannel)
    channel.channel_type = "web"
    channel.user_id = "bot"
    channel.name = "bot"

    context = ChatChannel._compose_context(
        channel,
        ContextType.TEXT,
        "edit this image into a poster",
        msg=_chat_msg(),
        isgroup=False,
    )

    assert context.type == ContextType.IMAGE_CREATE
    assert "[image:" in context.content
    assert record.image_path in context.content
    reset_image_recognition_manager(None)


def test_grok_text_to_image_does_not_attach_recent_image(monkeypatch, tmp_path):
    _patch_image_create_conf(monkeypatch)
    monkeypatch.setattr("channel.chat_channel.active_backend_is_grok_for_context", lambda ctx: True)

    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = ImageRecognitionManager(workspace_root=str(tmp_path / "workspace"), max_workers=1)
    reset_image_recognition_manager(manager)
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        manager.register_image(
            session_id="session",
            channel_type="web",
            image_path=str(source),
        )

    channel = object.__new__(ChatChannel)
    channel.channel_type = "web"
    channel.user_id = "bot"
    channel.name = "bot"

    context = ChatChannel._compose_context(
        channel,
        ContextType.TEXT,
        "generate an image of a cat",
        msg=_chat_msg(),
        isgroup=False,
    )

    assert context.type == ContextType.IMAGE_CREATE
    assert "[image:" not in context.content
    reset_image_recognition_manager(None)


def test_grok_text_to_image_opt_out_does_not_attach_recent_image(monkeypatch, tmp_path):
    _patch_image_create_conf(monkeypatch)
    monkeypatch.setattr("channel.chat_channel.active_backend_is_grok_for_context", lambda ctx: True)

    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = ImageRecognitionManager(workspace_root=str(tmp_path / "workspace"), max_workers=1)
    reset_image_recognition_manager(manager)
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        manager.register_image(
            session_id="session",
            channel_type="web",
            image_path=str(source),
        )

    channel = object.__new__(ChatChannel)
    channel.channel_type = "web"
    channel.user_id = "bot"
    channel.name = "bot"

    context = ChatChannel._compose_context(
        channel,
        ContextType.TEXT,
        "text-to-image generate an image of a cat without image",
        msg=_chat_msg(),
        isgroup=False,
    )

    assert context.type == ContextType.IMAGE_CREATE
    assert "[image:" not in context.content
    reset_image_recognition_manager(None)


def test_wecom_bot_declares_image_reply_supported():
    channel_cls = _singleton_class(WecomBotChannel)

    assert ReplyType.IMAGE not in channel_cls.NOT_SUPPORT_REPLYTYPE


def test_wecom_bot_sends_local_image_reply(monkeypatch, tmp_path):
    source = tmp_path / "source.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    sent_payloads = []
    upload_calls = []

    monkeypatch.setattr(channel, "_prepare_image_for_send", lambda path: path)
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: upload_calls.append((path, media_type)) or "mid")
    monkeypatch.setattr(channel, "_ws_send", lambda payload: sent_payloads.append(payload) or True)

    assert channel._send_image(str(source), "chat", False, req_id="req") is True
    assert upload_calls == [(str(source), "image")]
    assert sent_payloads[0]["body"]["msgtype"] == "image"
    assert sent_payloads[0]["body"]["image"]["media_id"] == "mid"


def _wecom_context():
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "chat"
    context["isgroup"] = False
    return context


def test_wecom_bot_generated_image_is_deleted_after_success(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = new_generated_media_path("xai_test", ".png")
    tmp_path.__class__(source).write_bytes(b"\x89PNG\r\n\x1a\nimage")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    reply = Reply(ReplyType.IMAGE, source)
    reply.cleanup_after_send = True

    monkeypatch.setattr(channel, "_reply_mention_target", lambda context: ([], []))
    monkeypatch.setattr(channel, "_prepare_image_for_send", lambda path: path)
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: "mid")
    monkeypatch.setattr(channel, "_ws_send", lambda payload: True)
    monkeypatch.setattr(channel, "_gen_req_id", lambda: "rid")

    assert channel.send(reply, _wecom_context()) is True
    assert not tmp_path.__class__(source).exists()


def test_wecom_bot_generated_image_is_deleted_after_upload_failure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = new_generated_media_path("xai_test", ".png")
    tmp_path.__class__(source).write_bytes(b"\x89PNG\r\n\x1a\nimage")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    reply = Reply(ReplyType.IMAGE, source)
    reply.cleanup_after_send = True

    monkeypatch.setattr(channel, "_reply_mention_target", lambda context: ([], []))
    monkeypatch.setattr(channel, "_prepare_image_for_send", lambda path: path)
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: "")
    monkeypatch.setattr(channel, "_send_text", lambda *args, **kwargs: True)

    assert channel.send(reply, _wecom_context()) is False
    assert not tmp_path.__class__(source).exists()
