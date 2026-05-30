import importlib.util
import json
from pathlib import Path

import pytest

from common import grok_image_prompt_rewriter
from integrations.hermes_xai import video_gen


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "skills" / "grok-video-generation" / "scripts" / "generate.py"


def load_generate_module():
    spec = importlib.util.spec_from_file_location("grok_video_generation_generate", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_grok_video_provider_rewrites_prompt_with_grok_model(monkeypatch, tmp_path):
    captured = {}
    rewrite_calls = []
    output = tmp_path / "result.mp4"
    output.write_bytes(b"\x00\x00\x00\x18ftypmp4")

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
    assert "repository_fragment_selection: already completed by deterministic local code" in rewrite_calls[0][1]
    assert "- none selected; use the system prompt template only" in rewrite_calls[0][1]
    assert provider.last_prompt_metadata["media_type"] == "video"
    assert provider.last_prompt_metadata["version"] == "grok-model-rewrite-v2"
    assert provider.last_prompt_metadata["library"]["name"] == "grok-video-generation"
    assert provider.last_prompt_metadata["supplements"] == []


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


def test_grok_video_provider_adds_reference_identity_lock_when_direct(monkeypatch, tmp_path):
    captured = {}
    reference = tmp_path / "ref.png"
    reference.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    output = tmp_path / "result.mp4"
    output.write_bytes(b"\x00\x00\x00\x18ftypmp4")

    def fail_rewrite(system_prompt, user_prompt):
        raise AssertionError("rewrite should be skipped")

    monkeypatch.setattr(grok_image_prompt_rewriter, "_call_grok_text_model", fail_rewrite)
    monkeypatch.setattr(video_gen, "_save_url_video", lambda *args, **kwargs: str(output))

    provider = video_gen.XAIVideoGenProvider()
    provider._submit = lambda payload: captured.setdefault("payload", payload) or "request-1"
    provider._poll = lambda request_id, **kwargs: {"video": {"url": "https://example.com/video.mp4"}}

    provider.generate("animate the background", image_url=str(reference), prompt_enhancement=False)

    prompt = captured["payload"]["prompt"]
    assert "animate the background" in prompt
    assert "Reference image identity lock:" in prompt
    assert "across all frames" in prompt
    assert "do not invent a new person" in prompt
    assert provider.last_prompt_metadata["enhanced_prompt"] == prompt


def test_grok_video_script_writes_prompt_metadata_for_history(monkeypatch, tmp_path):
    module = load_generate_module()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp4")
    output_dir = tmp_path / "out"

    class FakeProvider:
        def __init__(self):
            self.last_prompt_metadata = {
                "version": "grok-model-rewrite-v2",
                "enhanced": True,
                "target": "grok",
                "media_type": "video",
                "use_case": "video_model_rewrite",
                "original_prompt": "make a video",
                "enhanced_prompt": "Rewritten video prompt for history.",
                "library": {},
                "templates": [],
            }

        def generate(self, prompt, **kwargs):
            return str(source)

    monkeypatch.setattr(video_gen, "XAIVideoGenProvider", FakeProvider)

    result = module.GrokXAIVideoProvider().generate("make a video", output_dir=str(output_dir))

    metadata = json.loads((output_dir / "prompt_metadata.json").read_text(encoding="utf-8"))
    assert Path(result).name == "result.mp4"
    assert metadata["media_type"] == "video"
    assert metadata["enhanced_prompt"] == "Rewritten video prompt for history."


def test_grok_video_script_writes_prompt_metadata_when_provider_fails(monkeypatch, tmp_path):
    module = load_generate_module()
    output_dir = tmp_path / "out"

    class FakeProvider:
        def __init__(self):
            self.last_prompt_metadata = {
                "version": "grok-model-rewrite-v2",
                "enhanced": True,
                "target": "grok",
                "media_type": "video",
                "use_case": "video_model_rewrite",
                "original_prompt": "make a video",
                "enhanced_prompt": "Rewritten video prompt before failure.",
                "library": {},
                "templates": [],
            }

        def generate(self, prompt, **kwargs):
            raise RuntimeError("content policy rejected")

    monkeypatch.setattr(video_gen, "XAIVideoGenProvider", FakeProvider)

    with pytest.raises(RuntimeError, match="content policy rejected"):
        module.GrokXAIVideoProvider().generate("make a video", output_dir=str(output_dir))

    metadata = json.loads((output_dir / "prompt_metadata.json").read_text(encoding="utf-8"))
    assert metadata["media_type"] == "video"
    assert metadata["enhanced_prompt"] == "Rewritten video prompt before failure."


def test_grok_video_script_records_direct_prompt_metadata(monkeypatch, tmp_path):
    module = load_generate_module()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp4")
    output_dir = tmp_path / "out"

    class FakeProvider:
        def __init__(self):
            self.last_prompt_metadata = {
                "version": "grok-model-rewrite-v2",
                "enhanced": False,
                "disabled_reason": "disabled",
                "target": "grok",
                "media_type": "video",
                "use_case": "video_model_rewrite",
                "original_prompt": "raw video prompt",
                "enhanced_prompt": "raw video prompt",
                "library": {},
                "templates": [],
            }

        def generate(self, prompt, **kwargs):
            return str(source)

    monkeypatch.setattr(video_gen, "XAIVideoGenProvider", FakeProvider)

    result = module.GrokXAIVideoProvider().generate("raw video prompt", prompt_enhancement=False, output_dir=str(output_dir))

    metadata = json.loads((output_dir / "prompt_metadata.json").read_text(encoding="utf-8"))
    assert Path(result).name == "result.mp4"
    assert metadata["enhanced"] is False
    assert metadata["enhanced_prompt"] == "raw video prompt"
