# encoding:utf-8

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from channel.wechatcom.wechatcomapp_channel import WechatComAppChannel
from channel.wecom_bot.wecom_bot_channel import WecomBotChannel
from channel.weixin.weixin_channel import WeixinChannel
from integrations.hermes_xai.media_download import new_generated_media_path


def _singleton_class(factory):
    for cell in factory.__closure__ or []:
        value = cell.cell_contents
        if isinstance(value, type):
            return value
    raise AssertionError("singleton class not found")


def test_wecom_bot_video_reply_uploads_video_media(monkeypatch, tmp_path):
    source = tmp_path / "grok-video.mp4"
    source.write_bytes(b"0000ftypmp4 fake video")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    upload_calls = []
    sent_payloads = []
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "chat"
    context["isgroup"] = False

    monkeypatch.setattr(channel, "_reply_mention_target", lambda context: ([], []))
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: upload_calls.append((path, media_type)) or "mid")
    monkeypatch.setattr(channel, "_ws_send", lambda payload: sent_payloads.append(payload) or True)
    monkeypatch.setattr(channel, "_gen_req_id", lambda: "rid")

    assert channel.send(Reply(ReplyType.VIDEO, str(source)), context) is True
    assert upload_calls == [(str(source), "video")]
    assert sent_payloads[0]["body"]["msgtype"] == "video"
    assert sent_payloads[0]["body"]["video"]["media_id"] == "mid"


def test_wecom_bot_generated_video_is_deleted_after_success(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = new_generated_media_path("xai_test", ".mp4")
    tmp_path.__class__(source).write_bytes(b"0000ftypmp4 fake video")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "chat"
    context["isgroup"] = False
    reply = Reply(ReplyType.VIDEO, source)
    reply.cleanup_after_send = True

    monkeypatch.setattr(channel, "_reply_mention_target", lambda context: ([], []))
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: "mid")
    monkeypatch.setattr(channel, "_ws_send", lambda payload: True)
    monkeypatch.setattr(channel, "_gen_req_id", lambda: "rid")

    assert channel.send(reply, context) is True
    assert not tmp_path.__class__(source).exists()


def test_wecom_bot_generated_video_is_deleted_after_failure(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = new_generated_media_path("xai_test", ".mp4")
    tmp_path.__class__(source).write_bytes(b"0000ftypmp4 fake video")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "chat"
    context["isgroup"] = False
    reply = Reply(ReplyType.VIDEO, source)
    reply.cleanup_after_send = True

    monkeypatch.setattr(channel, "_reply_mention_target", lambda context: ([], []))
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: "")

    assert channel.send(reply, context) is False
    assert not tmp_path.__class__(source).exists()


def test_wechatcom_app_video_reply_uploads_and_sends_video(tmp_path):
    source = tmp_path / "grok-video.mp4"
    source.write_bytes(b"0000ftypmp4 fake video")

    class FakeMedia:
        def __init__(self):
            self.uploads = []

        def upload(self, media_type, handle):
            self.uploads.append((media_type, handle.name, handle.read()))
            return {"media_id": "mid"}

    class FakeMessage:
        def __init__(self):
            self.videos = []
            self.texts = []

        def send_video(self, agent_id, receiver, media_id):
            self.videos.append((agent_id, receiver, media_id))

        def send_text(self, agent_id, receiver, text):
            self.texts.append((agent_id, receiver, text))

    channel_cls = _singleton_class(WechatComAppChannel)
    channel = object.__new__(channel_cls)
    channel.agent_id = "agent"
    channel.client = type("FakeClient", (), {"media": FakeMedia(), "message": FakeMessage()})()
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "user"

    assert channel.send(Reply(ReplyType.VIDEO, str(source)), context) is True
    assert channel.client.media.uploads == [("video", str(source), b"0000ftypmp4 fake video")]
    assert channel.client.message.videos == [("agent", "user", "mid")]
    assert channel.client.message.texts == []


def test_wechatcom_app_generated_video_is_deleted_after_file_fallback(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    source = new_generated_media_path("xai_test", ".mp4")
    tmp_path.__class__(source).write_bytes(b"0000ftypmp4 fake video")

    class FakeMedia:
        def __init__(self):
            self.uploads = []

        def upload(self, media_type, handle):
            self.uploads.append((media_type, handle.name, handle.read()))
            return {"media_id": f"{media_type}-mid"}

    class FakeMessage:
        def __init__(self):
            self.files = []
            self.texts = []

        def send_file(self, agent_id, receiver, media_id):
            self.files.append((agent_id, receiver, media_id))

        def send_text(self, agent_id, receiver, text):
            self.texts.append((agent_id, receiver, text))

    channel_cls = _singleton_class(WechatComAppChannel)
    channel = object.__new__(channel_cls)
    channel.agent_id = "agent"
    channel.client = type("FakeClient", (), {"media": FakeMedia(), "message": FakeMessage()})()
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "user"
    reply = Reply(ReplyType.VIDEO, source)
    reply.cleanup_after_send = True

    assert channel.send(reply, context) is True
    assert channel.client.message.files == [("agent", "user", "file-mid")]
    assert not tmp_path.__class__(source).exists()


def test_wechatcom_app_video_reply_falls_back_when_file_missing():
    class FakeMedia:
        def upload(self, media_type, handle):
            raise AssertionError("missing video should not be uploaded")

    class FakeMessage:
        def __init__(self):
            self.texts = []

        def send_text(self, agent_id, receiver, text):
            self.texts.append((agent_id, receiver, text))

    channel_cls = _singleton_class(WechatComAppChannel)
    channel = object.__new__(channel_cls)
    channel.agent_id = "agent"
    channel.client = type("FakeClient", (), {"media": FakeMedia(), "message": FakeMessage()})()
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "user"

    assert channel.send(Reply(ReplyType.VIDEO, "missing-grok-video.mp4"), context) is False
    assert channel.client.message.texts == [
        ("agent", "user", "[Video send failed: file not found]")
    ]


def test_unmarked_non_generated_video_file_is_not_deleted(monkeypatch, tmp_path):
    source = tmp_path / "user-video.mp4"
    source.write_bytes(b"0000ftypmp4 fake video")

    channel_cls = _singleton_class(WecomBotChannel)
    channel = object.__new__(channel_cls)
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "chat"
    context["isgroup"] = False

    monkeypatch.setattr(channel, "_reply_mention_target", lambda context: ([], []))
    monkeypatch.setattr(channel, "_upload_media", lambda path, media_type: "mid")
    monkeypatch.setattr(channel, "_ws_send", lambda payload: True)
    monkeypatch.setattr(channel, "_gen_req_id", lambda: "rid")

    assert channel.send(Reply(ReplyType.VIDEO, str(source)), context) is True
    assert source.exists()


def test_weixin_video_reply_uses_video_upload_and_send(monkeypatch, tmp_path):
    source = tmp_path / "grok-video.mp4"
    source.write_bytes(b"0000ftypmp4 fake video")
    sent_videos = []
    upload_calls = []

    class FakeApi:
        def send_video_item(self, **payload):
            sent_videos.append(payload)

    channel = object.__new__(WeixinChannel)
    channel.api = FakeApi()
    channel._context_tokens = {"user": "token"}
    context = Context(ContextType.TEXT, "")
    context["receiver"] = "user"

    def fake_upload(api, local_path, receiver, media_type):
        upload_calls.append((api, local_path, receiver, media_type))
        return {
            "encrypt_query_param": "eq",
            "aes_key_b64": "key",
            "ciphertext_size": 123,
        }

    monkeypatch.setattr(channel, "_ensure_api", lambda: True)
    monkeypatch.setattr("channel.weixin.weixin_channel.upload_media_to_cdn", fake_upload)

    assert channel.send(Reply(ReplyType.VIDEO, str(source)), context) is True
    assert upload_calls == [(channel.api, str(source), "user", 2)]
    assert sent_videos == [
        {
            "to": "user",
            "context_token": "token",
            "encrypt_query_param": "eq",
            "aes_key_b64": "key",
            "ciphertext_size": 123,
        }
    ]
