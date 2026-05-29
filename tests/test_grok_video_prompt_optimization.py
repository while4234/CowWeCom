from common import grok_image_prompt_rewriter
from integrations.hermes_xai import video_gen


def test_grok_video_provider_rewrites_prompt_with_grok_model(monkeypatch, tmp_path):
    captured = {}
    output = tmp_path / "result.mp4"
    output.write_bytes(b"\x00\x00\x00\x18ftypmp4")

    monkeypatch.setattr(
        grok_image_prompt_rewriter,
        "_call_grok_text_model",
        lambda system_prompt, user_prompt: "Rewritten Grok video prompt.",
    )
    monkeypatch.setattr(video_gen, "_save_url_video", lambda *args, **kwargs: str(output))

    provider = video_gen.XAIVideoGenProvider()
    provider._submit = lambda payload: captured.setdefault("payload", payload) or "request-1"
    provider._poll = lambda request_id, **kwargs: {"video": {"url": "https://example.com/video.mp4"}}

    path = provider.generate("grokSfw 生成一个城市延时摄影视频", duration="6s")

    assert path == str(output)
    assert captured["payload"]["prompt"] == "Rewritten Grok video prompt."
    assert provider.last_prompt_metadata["media_type"] == "video"
    assert provider.last_prompt_metadata["version"] == "grok-model-rewrite-v2"


def test_grok_video_provider_can_skip_prompt_rewrite(monkeypatch, tmp_path):
    captured = {}
    output = tmp_path / "result.mp4"
    output.write_bytes(b"\x00\x00\x00\x18ftypmp4")

    def fail_rewrite(system_prompt, user_prompt):
        raise AssertionError("rewrite should be skipped")

    monkeypatch.setattr(grok_image_prompt_rewriter, "_call_grok_text_model", fail_rewrite)
    monkeypatch.setattr(video_gen, "_save_url_video", lambda *args, **kwargs: str(output))

    provider = video_gen.XAIVideoGenProvider()
    provider._submit = lambda payload: captured.setdefault("payload", payload) or "request-1"
    provider._poll = lambda request_id, **kwargs: {"video": {"url": "https://example.com/video.mp4"}}

    provider.generate("raw video prompt", prompt_enhancement=False)

    assert captured["payload"]["prompt"] == "raw video prompt"
    assert provider.last_prompt_metadata["enhanced"] is False
