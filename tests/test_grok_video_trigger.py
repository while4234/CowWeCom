# encoding:utf-8

from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from bridge.context import Context, ContextType
from bridge.reply import ReplyType
from channel.channel import Channel
from channel.chat_channel import ChatChannel
from channel.image_recognition import ImageRecognitionManager, reset_image_recognition_manager
from channel.wecom_bot.wecom_bot_channel import WecomBotChannel
from models.grok.grok_bot import GrokBot


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def _patch_grok_video_background(monkeypatch):
    manager = SimpleNamespace(
        submitted=[],
        submit=lambda args, context, profile: manager.submitted.append((args, context, profile))
        or SimpleNamespace(job_id="video-job"),
        queue_position=lambda job: 0,
    )
    monkeypatch.setattr(
        "agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager",
        lambda agent_bridge=None: manager,
    )
    monkeypatch.setattr("bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None))
    monkeypatch.setattr(
        "models.grok.grok_video._resolve_background_profile",
        lambda context: SimpleNamespace(actor_id="actor", memory_user_id="user"),
    )
    return manager


def test_grok_bot_video_create_starts_background_task(monkeypatch):
    manager = SimpleNamespace(
        submitted=[],
        submit=lambda args, context, profile: manager.submitted.append((args, context, profile))
        or SimpleNamespace(job_id="video-job"),
        queue_position=lambda job: 0,
    )
    monkeypatch.setattr(
        "agent.tools.video_generation.job_manager.get_grok_video_generation_job_manager",
        lambda agent_bridge=None: manager,
    )
    monkeypatch.setattr("bridge.bridge.Bridge", lambda: SimpleNamespace(get_agent_bridge=lambda: None))
    monkeypatch.setattr(
        "models.grok.grok_video._resolve_background_profile",
        lambda context: SimpleNamespace(actor_id="actor", memory_user_id="user"),
    )

    context = Context(ContextType.VIDEO_CREATE, "make a red kite video")
    reply = object.__new__(GrokBot).reply(context.content, context)

    assert reply.type == ReplyType.TEXT
    assert "video-job" in reply.content
    assert manager.submitted[0][0]["prompt"] == "make a red kite video"


def test_grok_bot_video_create_extracts_natural_resolution_and_duration(monkeypatch):
    manager = _patch_grok_video_background(monkeypatch)
    context = Context(ContextType.VIDEO_CREATE, "生成视频 720p 10s 让镜头缓慢推进")

    reply = object.__new__(GrokBot).reply(context.content, context)

    assert reply.type == ReplyType.TEXT
    args = manager.submitted[0][0]
    assert args["prompt"] == "生成视频 720p 10s 让镜头缓慢推进"
    assert args["resolution"] == "720p"
    assert args["duration"] == "10s"


def test_grok_bot_video_create_uses_context_default_duration(monkeypatch):
    manager = _patch_grok_video_background(monkeypatch)
    context = Context(ContextType.VIDEO_CREATE, "make a cat video")
    context["grok_video_default_duration"] = "10s"

    reply = object.__new__(GrokBot).reply(context.content, context)

    assert reply.type == ReplyType.TEXT
    assert manager.submitted[0][0]["duration"] == "10s"


def test_default_agent_mode_video_create_shortcuts_to_grok_video(monkeypatch):
    context = Context(ContextType.VIDEO_CREATE, "make a cat video")
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "agent": True,
        "video_generation_provider": "xai",
    }.get(key, default)
    called = []

    class TestChannel(Channel):
        channel_type = "wecom_bot"

    def fake_fetch(query, ctx):
        called.append((query, ctx))
        return "video-reply"

    monkeypatch.setattr("channel.channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.channel.Bridge", lambda: SimpleNamespace(fetch_reply_content=fake_fetch))

    assert TestChannel().build_reply_content("make a cat video", context) == "video-reply"
    assert called == [("make a cat video", context)]


def test_active_grok_profile_video_create_shortcuts_without_global_provider(monkeypatch):
    context = Context(ContextType.VIDEO_CREATE, "make a cat video")
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "agent": True,
        "video_generation_provider": "none",
    }.get(key, default)
    called = []

    class TestChannel(Channel):
        channel_type = "wecom_bot"

    def fake_fetch(query, ctx):
        called.append((query, ctx))
        return "grok-video-reply"

    monkeypatch.setattr("channel.channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.channel.active_backend_is_grok_for_context", lambda ctx: True)
    monkeypatch.setattr("models.grok.grok_video.is_grok_video_provider", lambda: False)
    monkeypatch.setattr("channel.channel.Bridge", lambda: SimpleNamespace(fetch_reply_content=fake_fetch))

    assert TestChannel().build_reply_content("make a cat video", context) == "grok-video-reply"
    assert called == [("make a cat video", context)]


def test_non_grok_video_create_keeps_agent_mode(monkeypatch):
    context = Context(ContextType.VIDEO_CREATE, "make normally")
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "agent": True,
        "video_generation_provider": "none",
    }.get(key, default)
    called = []

    class TestChannel(Channel):
        channel_type = "web"

    def fake_agent(query, context, on_event=None, clear_history=False):
        called.append((query, context, clear_history))
        return "agent-reply"

    monkeypatch.setattr("channel.channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.channel.Bridge", lambda: SimpleNamespace(fetch_agent_reply=fake_agent))

    assert TestChannel().build_reply_content("make normally", context) == "agent-reply"
    assert called == [("make normally", context, False)]


def test_video_prefix_is_checked_before_image_prefix(monkeypatch):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "single_chat_prefix": [""],
        "video_create_prefix": ["画个视频"],
        "image_create_prefix": ["画"],
        "nick_name_black_list": [],
        "always_reply_voice": False,
        "trigger_by_self": True,
    }.get(key, default)
    fake_conf.get_user_data.return_value = {}
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)

    channel = object.__new__(ChatChannel)
    channel.channel_type = "wecom_bot"
    channel.user_id = "bot"
    channel.name = "bot"
    msg = SimpleNamespace(
        from_user_id="u1",
        from_user_nickname="User",
        other_user_id="u1",
        other_user_nickname="User",
        to_user_id="bot",
        actual_user_id="u1",
        actual_user_nickname="User",
        is_at=False,
        at_list=[],
        self_display_name="bot",
    )

    context = ChatChannel._compose_context(
        channel,
        ContextType.TEXT,
        "画个视频 让城市动起来",
        msg=msg,
        isgroup=False,
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert context.content == "让城市动起来"


def _wecom_msg():
    return SimpleNamespace(
        input_is_voice=False,
        source_msgtype="text",
        is_group=False,
        from_user_id="u1",
        from_user_nickname="User",
        other_user_id="u1",
        other_user_nickname="User",
        to_user_id="bot",
        actual_user_id="u1",
        actual_user_nickname="User",
    )


def test_wecom_bot_video_prefix_creates_video_context(monkeypatch):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": ["生成视频"],
        "image_create_prefix": ["画图"],
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    context = channel._compose_context(
        ContextType.TEXT,
        "生成视频：一只猫跑步",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert context.content == "一只猫跑步"
    assert context["receiver"] == "u1"
    assert context["session_id"] == "u1"
    assert context["_visible_task_summary"] == "一只猫跑步"


def test_wecom_bot_image_to_video_uses_recent_image_ref(monkeypatch, tmp_path):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": [],
        "image_create_prefix": [],
        "background_image_recognition_enabled": True,
        "image_recognition_recent_video_ref_window_seconds": 600,
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)

    workspace = tmp_path / "workspace"
    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(manager)
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        record = manager.register_image(
            session_id="u1",
            channel_type="wecom_bot",
            image_path=str(source),
        )

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    context = channel._compose_context(
        ContextType.TEXT,
        "generate a video image to video wave",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert context["_visible_task_summary"] == "image to video wave"
    assert "[image:" in context.content
    assert record.image_path in context.content
    reset_image_recognition_manager(None)


def test_wecom_bot_video_prefix_after_recent_image_auto_uses_ref(monkeypatch, tmp_path):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": ["生成视频"],
        "image_create_prefix": [],
        "background_image_recognition_enabled": True,
        "image_recognition_video_create_auto_ref_window_seconds": 120,
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)

    workspace = tmp_path / "workspace"
    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(manager)
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        record = manager.register_image(
            session_id="u1",
            channel_type="wecom_bot",
            image_path=str(source),
        )

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    context = channel._compose_context(
        ContextType.TEXT,
        "生成视频：镜头缓慢推进",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert context["_visible_task_summary"] == "镜头缓慢推进"
    assert "[image:" in context.content
    assert record.image_path in context.content
    reset_image_recognition_manager(None)


def test_wecom_bot_image_to_video_uses_requested_recent_image_count(monkeypatch, tmp_path):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": [],
        "image_create_prefix": [],
        "background_image_recognition_enabled": True,
        "image_recognition_recent_video_ref_window_seconds": 600,
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)

    workspace = tmp_path / "workspace"
    manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(manager)
    records = []
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        for name in ("first.png", "second.png", "third.png"):
            source = tmp_path / name
            source.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode("ascii"))
            records.append(
                manager.register_image(
                    session_id="u1",
                    channel_type="wecom_bot",
                    image_path=str(source),
                )
            )

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    context = channel._compose_context(
        ContextType.TEXT,
        "generate a video 参考上面2张图片生成产品视频",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert records[0].image_path not in context.content
    assert records[1].image_path in context.content
    assert records[2].image_path in context.content
    assert context.content.count("[image:") == 2
    reset_image_recognition_manager(None)


def test_wecom_bot_image_to_video_without_count_uses_latest_image(monkeypatch, tmp_path):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": [],
        "image_create_prefix": [],
        "background_image_recognition_enabled": True,
        "image_recognition_recent_video_ref_window_seconds": 600,
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)

    workspace = tmp_path / "workspace"
    manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(manager)
    records = []
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        for name in ("first.png", "second.png"):
            source = tmp_path / name
            source.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode("ascii"))
            records.append(
                manager.register_image(
                    session_id="u1",
                    channel_type="wecom_bot",
                    image_path=str(source),
                )
            )

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    context = channel._compose_context(
        ContextType.TEXT,
        "generate a video 参考上面的图片生成产品视频",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert records[0].image_path not in context.content
    assert records[1].image_path in context.content
    assert context.content.count("[image:") == 1
    reset_image_recognition_manager(None)


def test_wecom_bot_video_prefix_without_hint_uses_latest_recent_image(monkeypatch, tmp_path):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": ["生成视频"],
        "image_create_prefix": [],
        "background_image_recognition_enabled": True,
        "image_recognition_video_create_auto_ref_window_seconds": 120,
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)

    workspace = tmp_path / "workspace"
    manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(manager)
    records = []
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        for name in ("first.png", "second.png"):
            source = tmp_path / name
            source.write_bytes(b"\x89PNG\r\n\x1a\n" + name.encode("ascii"))
            records.append(
                manager.register_image(
                    session_id="u1",
                    channel_type="wecom_bot",
                    image_path=str(source),
                )
            )

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    context = channel._compose_context(
        ContextType.TEXT,
        "生成视频：10s 让镜头轻微推进",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert records[0].image_path not in context.content
    assert records[1].image_path in context.content
    assert context.content.count("[image:") == 1
    reset_image_recognition_manager(None)


def test_grok_video_create_detaches_from_session_queue(monkeypatch):
    import threading

    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "concurrency_in_session": 1,
        "video_generation_provider": "xai",
    }.get(key, default)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("models.grok.grok_video.is_grok_video_provider", lambda: True)
    sent = []

    channel = object.__new__(ChatChannel)
    channel.channel_type = "web"
    channel.lock = threading.RLock()
    channel.sessions = {}
    channel.futures = {}
    channel._handle_detached_grok_video_create = lambda context: sent.append(context.content)

    context = Context(ContextType.VIDEO_CREATE, "生成视频 10s")
    context["session_id"] = "session"

    channel.produce(context)

    assert sent == ["生成视频 10s"]
    assert "session" not in channel.sessions


def test_wecom_bot_text_to_video_opt_out_skips_recent_image_ref(monkeypatch, tmp_path):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": ["生成视频"],
        "image_create_prefix": [],
        "background_image_recognition_enabled": True,
        "image_recognition_video_create_auto_ref_window_seconds": 120,
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)
    monkeypatch.setattr("channel.chat_channel.conf", lambda: fake_conf)

    workspace = tmp_path / "workspace"
    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(manager)
    with patch.object(ImageRecognitionManager, "_recognize_image", return_value="summary"):
        manager.register_image(
            session_id="u1",
            channel_type="wecom_bot",
            image_path=str(source),
        )

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    context = channel._compose_context(
        ContextType.TEXT,
        "生成视频：文生视频，一只猫跑步",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert context.type == ContextType.VIDEO_CREATE
    assert context.content == "文生视频，一只猫跑步"
    reset_image_recognition_manager(None)


def test_wecom_bot_image_prefix_and_plain_text_still_work(monkeypatch):
    fake_conf = MagicMock()
    fake_conf.get.side_effect = lambda key, default=None: {
        "video_create_prefix": ["生成视频"],
        "image_create_prefix": ["画图"],
    }.get(key, default)
    monkeypatch.setattr("channel.wecom_bot.wecom_bot_channel.conf", lambda: fake_conf)

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    channel.channel_type = "wecom_bot"

    image_context = channel._compose_context(
        ContextType.TEXT,
        "画图：一只猫",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )
    text_context = channel._compose_context(
        ContextType.TEXT,
        "普通文本",
        msg=_wecom_msg(),
        isgroup=False,
        no_need_at=True,
    )

    assert image_context.type == ContextType.IMAGE_CREATE
    assert image_context.content == "一只猫"
    assert text_context.type == ContextType.TEXT
    assert text_context.content == "普通文本"


def test_grok_video_uses_recent_image_count(monkeypatch, tmp_path):
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    third = tmp_path / "c.png"
    for path in (first, second, third):
        path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = _patch_grok_video_background(monkeypatch)
    prompt = (
        f"参考上面发的2张图片生成产品视频\n"
        f"[图片: {first}]\n[图片: {second}]\n[图片: {third}]"
    )

    reply = object.__new__(GrokBot).reply(prompt, Context(ContextType.VIDEO_CREATE, prompt))

    assert reply.type == ReplyType.TEXT
    args = manager.submitted[0][0]
    assert args["image_url"] == [str(second), str(third)]
    assert "aspect_ratio" not in args


def test_grok_video_reference_request_without_count_uses_latest_image(monkeypatch, tmp_path):
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    third = tmp_path / "c.png"
    for path in (first, second, third):
        path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = _patch_grok_video_background(monkeypatch)
    prompt = (
        f"参考上面的图片生成产品视频\n"
        f"[图片: {first}]\n[图片: {second}]\n[图片: {third}]"
    )

    reply = object.__new__(GrokBot).reply(prompt, Context(ContextType.VIDEO_CREATE, prompt))

    assert reply.type == ReplyType.TEXT
    args = manager.submitted[0][0]
    assert args["image_url"] == str(third)


def test_grok_video_with_refs_without_count_uses_latest_image(monkeypatch, tmp_path):
    first = tmp_path / "a.png"
    second = tmp_path / "b.png"
    for path in (first, second):
        path.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    manager = _patch_grok_video_background(monkeypatch)
    prompt = f"背景换成火星\n[图片: {first}]\n[图片: {second}]"

    reply = object.__new__(GrokBot).reply(prompt, Context(ContextType.VIDEO_CREATE, prompt))

    assert reply.type == ReplyType.TEXT
    args = manager.submitted[0][0]
    assert args["image_url"] == str(second)


def test_grok_video_reference_request_without_image_fails():
    context = Context(ContextType.VIDEO_CREATE, "参考上面发的图片生成视频")
    reply = object.__new__(GrokBot).reply(context.content, context)

    assert reply.type == ReplyType.ERROR
    assert "没有找到可用图片" in reply.content


def test_grok_video_without_hint_uses_latest_recent_image(monkeypatch, tmp_path):
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
    job_manager = _patch_grok_video_background(monkeypatch)
    context = Context(ContextType.VIDEO_CREATE, "生成视频 10s 让镜头推进")
    context["session_id"] = "session"

    reply = object.__new__(GrokBot).reply(context.content, context)

    assert reply.type == ReplyType.TEXT
    assert job_manager.submitted[0][0]["image_url"] == record.image_path
    reset_image_recognition_manager(None)


def test_grok_video_text_to_video_opt_out_ignores_latest_recent_image(monkeypatch, tmp_path):
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
    job_manager = _patch_grok_video_background(monkeypatch)
    context = Context(ContextType.VIDEO_CREATE, "文生视频，一只猫在月球奔跑")
    context["session_id"] = "session"

    reply = object.__new__(GrokBot).reply(context.content, context)

    assert reply.type == ReplyType.TEXT
    assert "image_url" not in job_manager.submitted[0][0]
    assert job_manager.submitted[0][0]["aspect_ratio"] == "16:9"
    reset_image_recognition_manager(None)
