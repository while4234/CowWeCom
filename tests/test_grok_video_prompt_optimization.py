from common import grok_image_prompt_rewriter
from integrations.hermes_xai import video_gen


class FixedRandom:
    def __init__(self, roll):
        self.roll = roll

    def random(self):
        return self.roll

    def choice(self, values):
        return values[0]


def write_grok_repository_skill(root):
    skill_dir = root / "image-prompt-optimization"
    repositories = skill_dir / "repositories"
    (repositories / "grok").mkdir(parents=True)
    (repositories / "general").mkdir()
    (skill_dir / "SKILL.md").write_text("# image-prompt-optimization\n", encoding="utf-8")
    (repositories / "grok" / "video.txt").write_text("grok camera motion detail\n", encoding="utf-8")
    (repositories / "general" / "video.txt").write_text("general camera motion detail\n", encoding="utf-8")
    return skill_dir


def test_grok_video_provider_rewrites_prompt_with_grok_model(monkeypatch, tmp_path):
    captured = {}
    rewrite_calls = []
    skill_dir = write_grok_repository_skill(tmp_path)
    output = tmp_path / "result.mp4"
    output.write_bytes(b"\x00\x00\x00\x18ftypmp4")

    monkeypatch.setenv("IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR", str(skill_dir))
    monkeypatch.setattr("common.prompt_optimization_repository.random.SystemRandom", lambda: FixedRandom(0.1))

    def fake_rewrite(system_prompt, user_prompt):
        rewrite_calls.append((system_prompt, user_prompt))
        return "Rewritten Grok video prompt."

    monkeypatch.setattr(grok_image_prompt_rewriter, "_call_grok_text_model", fake_rewrite)
    monkeypatch.setattr(video_gen, "_save_url_video", lambda *args, **kwargs: str(output))

    provider = video_gen.XAIVideoGenProvider()
    provider._submit = lambda payload: captured.setdefault("payload", payload) or "request-1"
    provider._poll = lambda request_id, **kwargs: {"video": {"url": "https://example.com/video.mp4"}}

    path = provider.generate("generate a city timelapse video", duration="6s")

    assert path == str(output)
    assert captured["payload"]["prompt"] == "Rewritten Grok video prompt."
    assert "matched_prompt_repository_keyword: grok" in rewrite_calls[0][1]
    assert "repository_selection_rule: 90% from grok, 10% from other repositories" in rewrite_calls[0][1]
    assert "[grok/video.txt:1] grok camera motion detail" in rewrite_calls[0][1]
    assert provider.last_prompt_metadata["media_type"] == "video"
    assert provider.last_prompt_metadata["version"] == "grok-model-rewrite-v2"
    assert provider.last_prompt_metadata["library"]["keyword"] == "grok"
    assert provider.last_prompt_metadata["supplements"][0]["repository"] == "grok"


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
