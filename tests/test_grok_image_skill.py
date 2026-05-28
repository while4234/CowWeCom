import importlib.util
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from agent.tools.image_generation.image_generation_task import ImageGenerationTaskTool
from agent.tools.image_generation.job_manager import ImageGenerationJobManager
from bridge.context import Context, ContextType


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = PROJECT_ROOT / "skills" / "image-generation" / "scripts" / "generate.py"


def load_generate_module():
    spec = importlib.util.spec_from_file_location("image_generation_generate_grok", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class CaptureManager:
    def __init__(self):
        self.submitted = []

    def submit(self, args, context, profile):
        self.submitted.append(dict(args))
        return SimpleNamespace(job_id="grokjob123")

    def queue_position(self, job):
        return 0


def make_context():
    context = Context(ContextType.TEXT, "draw")
    context["receiver"] = "receiver"
    context["session_id"] = "receiver"
    return context


def make_profile(root: str):
    return SimpleNamespace(actor_id="user", memory_user_id="user", shared_workspace=root)


class TestGrokImageSkill(unittest.TestCase):
    def test_tool_schema_keeps_grok_runtime_explicit_only(self):
        runtime = ImageGenerationTaskTool.params["properties"]["runtime"]

        self.assertIn("default Codex", runtime["description"])
        self.assertIn("explicitly asks for Grok", runtime["description"])
        self.assertIn("Do not pass grok just because", runtime["description"])

    def test_tool_passes_explicit_grok_runtime_to_background_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CaptureManager()
            tool = ImageGenerationTaskTool()
            tool.job_manager = manager
            tool.current_context = make_context()
            tool.profile = make_profile(tmp)

            result = tool.execute({
                "prompt": "Use Grok to generate a product poster",
                "runtime": "grok",
                "quality": "high",
            })

            self.assertEqual(result.status, "success")
            self.assertEqual(manager.submitted[0]["runtime"], "grok")
            self.assertEqual(manager.submitted[0]["quality"], "high")

    def test_job_manager_preserves_default_codex_runtime_when_runtime_omitted(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = ImageGenerationJobManager(script_path=str(SCRIPT), workspace_root=tmp, global_workers=1)
            try:
                cleaned = manager._clean_args({"prompt": "high quality quick sticker", "quality": "high"})
            finally:
                manager.shutdown(wait=False)

            self.assertEqual(cleaned["runtime"], "codex_auth")
            self.assertNotEqual(cleaned["runtime"], "grok")

    def test_generate_script_builds_grok_provider_only_for_grok_runtime(self):
        module = load_generate_module()

        self.assertTrue(module._is_grok_runtime("grok"))
        self.assertTrue(module._is_grok_runtime("xai"))
        providers = module._build_providers("", runtime="grok")
        self.assertEqual([(label, type(provider).__name__) for label, provider in providers], [("GrokXAI", "GrokXAIProvider")])

    def test_grok_model_selection_keeps_fast_default_until_quality_is_explicit(self):
        module = load_generate_module()

        self.assertEqual(module._resolve_grok_model("simple square", quality="high"), "grok-imagine-image-quality")
        self.assertEqual(module._resolve_grok_model("quick draft sticker", quality="speed"), "grok-imagine-image")
        self.assertEqual(module._resolve_grok_model("photorealistic product poster"), "grok-imagine-image")
        self.assertEqual(module._resolve_grok_model("grok high quality product poster"), "grok-imagine-image-quality")
        self.assertEqual(module._resolve_grok_model("simple meme sticker"), "grok-imagine-image")

    def test_grok_provider_uses_hermes_provider_and_copies_local_output(self):
        module = load_generate_module()
        calls = []

        class FakeXAIImageGenProvider:
            def generate(self, prompt, *, aspect_ratio=None, resolution=None, model=None):
                calls.append({
                    "prompt": prompt,
                    "aspect_ratio": aspect_ratio,
                    "resolution": resolution,
                    "model": model,
                })
                source = Path(output_tmp) / "source.jpg"
                source.write_bytes(b"\xff\xd8\xff\xe0fake-jpeg")
                return str(source)

        from integrations.hermes_xai import image_gen as xai_image_gen

        with tempfile.TemporaryDirectory() as tmp:
            output_tmp = tmp
            original = xai_image_gen.XAIImageGenProvider
            xai_image_gen.XAIImageGenProvider = FakeXAIImageGenProvider
            try:
                provider = module.GrokXAIProvider()
                paths = provider.generate(
                    "Use Grok for a high quality product poster",
                    size="2K",
                    aspect_ratio="3:4",
                    output_dir=tmp,
                )
            finally:
                xai_image_gen.XAIImageGenProvider = original

            self.assertEqual(len(paths), 1)
            self.assertTrue(Path(paths[0]).exists())
            self.assertEqual(Path(paths[0]).parent, Path(tmp).resolve())
            self.assertEqual(calls[0]["resolution"], "2k")
            self.assertEqual(calls[0]["aspect_ratio"], "3:4")
            self.assertEqual(calls[0]["model"], "grok-imagine-image-quality")

    def test_grok_skill_doc_forbids_quality_only_provider_switch(self):
        text = (PROJECT_ROOT / "skills" / "grok-image-generation" / "SKILL.md").read_text(encoding="utf-8")

        self.assertIn("If the user only says high quality", text)
        self.assertIn("keep the default Codex runtime", text)
        self.assertIn('"runtime": "grok"', text)


if __name__ == "__main__":
    unittest.main()
