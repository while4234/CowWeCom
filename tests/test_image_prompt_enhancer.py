import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from agent.tools.image_generation import prompt_history as prompt_history_module
from agent.tools.image_generation.prompt_history import ImageGenerationPromptHistoryTool
from bridge.context import Context, ContextType
from common.image_prompt_enhancer import (
    ENHANCED_PROMPT_MARKER,
    enhance_image_prompt,
    load_prompt_history,
    record_prompt_history,
    redact_hidden_image_prompt_text,
    write_prompt_metadata,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]
REFERENCE_ROOT = PROJECT_ROOT / "skills" / "image-prompt-optimization" / "references" / "nano-banana-pro"


def write_fixture_library(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    manifest = {
        "updatedAt": "2026-05-28T04:13:26.486Z",
        "totalPrompts": 10001,
        "categories": [
            {"slug": "profile-avatar", "title": "Profile / Avatar", "file": "profile-avatar.json", "count": 1},
            {"slug": "infographic-edu-visual", "title": "Infographic / Edu Visual", "file": "infographic-edu-visual.json", "count": 1},
            {"slug": "poster-flyer", "title": "Poster / Flyer", "file": "poster-flyer.json", "count": 1},
            {"slug": "others", "title": "Uncategorized", "file": "others.json", "count": 1},
        ],
    }
    (root / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (root / "profile-avatar.json").write_text(
        json.dumps(
            [
                {
                    "id": 1,
                    "title": "Editorial portrait with soft studio lighting",
                    "description": "A refined portrait template with lens, skin texture, outfit, pose, and mood.",
                    "content": "Subject identity, natural skin texture, 85mm lens, soft key light, tasteful wardrobe.",
                    "sourceMedia": ["https://example.com/portrait.jpg"],
                    "needReferenceImages": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    (root / "infographic-edu-visual.json").write_text(
        json.dumps(
            [
                {
                    "id": 2,
                    "title": "Clean process flowchart infographic",
                    "description": "A premium infographic template for process diagrams and readable steps.",
                    "content": "Flowchart, numbered steps, clear arrows, clean bento layout, legible labels.",
                    "sourceMedia": ["https://example.com/flow.jpg"],
                    "needReferenceImages": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    (root / "poster-flyer.json").write_text(
        json.dumps(
            [
                {
                    "id": 3,
                    "title": "Modern launch event poster",
                    "description": "A poster template with clear hierarchy and campaign visuals.",
                    "content": "Hero title, supporting copy, bold composition, print-ready poster layout.",
                    "sourceMedia": ["https://example.com/poster.jpg"],
                    "needReferenceImages": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    (root / "others.json").write_text("[]", encoding="utf-8")


class FixedRandom:
    def __init__(self, roll):
        self.roll = roll

    def random(self):
        return self.roll

    def choice(self, values):
        return values[0]


def write_grok_repository_skill(root: Path):
    skill_dir = root / "image-prompt-optimization"
    repositories = skill_dir / "repositories"
    (repositories / "grok").mkdir(parents=True)
    (repositories / "general").mkdir()
    (skill_dir / "SKILL.md").write_text("# image-prompt-optimization\n", encoding="utf-8")
    (repositories / "grok" / "visual.txt").write_text("grok cinematic detail\n", encoding="utf-8")
    (repositories / "general" / "visual.txt").write_text("general cinematic detail\n", encoding="utf-8")
    return skill_dir


class TestImagePromptEnhancer(unittest.TestCase):
    def test_full_prompt_library_reference_snapshot_is_present(self):
        manifest = json.loads((REFERENCE_ROOT / "manifest.json").read_text(encoding="utf-8"))

        self.assertGreaterEqual(manifest["totalPrompts"], 10000)
        self.assertTrue((REFERENCE_ROOT / "profile-avatar.json").exists())
        self.assertTrue((REFERENCE_ROOT / "social-media-post.json").exists())

    def test_grok_uses_model_rewrite_instead_of_prompt_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            skill_dir = write_grok_repository_skill(Path(tmp))

            with patch.dict(os.environ, {"IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR": str(skill_dir)}), patch(
                "common.prompt_optimization_repository.random.SystemRandom",
                return_value=FixedRandom(0.1),
            ), patch(
                "common.grok_image_prompt_rewriter._call_grok_text_model",
                return_value="Final prompt\uff1a A polished Grok prompt with cinematic lighting.",
            ) as rewrite_call:
                result = enhance_image_prompt(
                    "generate a calm cinematic lake",
                    target="grok",
                    model="grok-imagine-image",
                )

        self.assertTrue(result["enhanced"])
        self.assertEqual(result["version"], "grok-model-rewrite-v2")
        self.assertEqual(result["use_case"], "image_model_rewrite")
        self.assertEqual(result["enhanced_prompt"], "A polished Grok prompt with cinematic lighting.")
        self.assertEqual(result["templates"], [])
        self.assertEqual(result["library"]["name"], "image-prompt-optimization")
        self.assertEqual(result["library"]["keyword"], "grok")
        self.assertEqual(result["supplements"][0]["repository"], "grok")
        self.assertEqual(rewrite_call.call_count, 1)
        system_prompt, user_prompt = rewrite_call.call_args.args
        self.assertIn("Grok image prompt optimizer", system_prompt)
        self.assertIn("generate a calm cinematic lake", user_prompt)
        self.assertIn("matched_prompt_repository_keyword: grok", user_prompt)
        self.assertIn("repository_selection_rule: 90% from grok, 10% from other repositories", user_prompt)
        self.assertIn("[grok/visual.txt:1] grok cinematic detail", user_prompt)

    def test_global_disable_skips_grok_model_rewrite(self):
        with patch.dict(os.environ, {"IMAGE_PROMPT_ENHANCEMENT_ENABLED": "false"}):
            with patch("common.grok_image_prompt_rewriter._call_grok_text_model") as rewrite_call:
                result = enhance_image_prompt(
                    "grok 帮我生成一张高级感人物写真",
                    target="grok",
                    model="grok-imagine-image",
                    enabled=True,
                )

        self.assertFalse(result["enhanced"])
        self.assertEqual(result["disabled_reason"], "disabled")
        self.assertEqual(result["enhanced_prompt"], "grok 帮我生成一张高级感人物写真")
        rewrite_call.assert_not_called()

    def test_gpt_routes_flowcharts_to_infographic_prompt_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            write_fixture_library(library)

            result = enhance_image_prompt(
                "画一个从用户输入到模型输出的流程图",
                target="gpt",
                model="gpt-image-2",
                library_dir=str(library),
            )

        self.assertTrue(result["enhanced"])
        self.assertEqual(result["use_case"], "infographic")
        self.assertIn("GPT image prompt", result["enhanced_prompt"])
        self.assertEqual(result["templates"][0]["category_slug"], "infographic-edu-visual")

    def test_prompt_metadata_and_history_are_hidden_until_tool_reads_them(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp) / "library"
            write_fixture_library(library)
            output_dir = Path(tmp) / "out"
            metadata = enhance_image_prompt(
                "生成一个产品海报",
                target="gpt",
                model="gpt-image-2",
                library_dir=str(library),
            )
            write_prompt_metadata(str(output_dir), metadata)
            history = record_prompt_history(
                workspace_root=tmp,
                memory_user_id="user_a",
                session_id="session-a",
                job_id="job123",
                output_path=str(output_dir / "out.png"),
                metadata=metadata,
            )

            self.assertTrue(Path(history).exists())
            loaded = load_prompt_history(
                workspace_root=tmp,
                memory_user_id="user_a",
                session_id="session-a",
                limit=1,
            )
            self.assertEqual(loaded[0]["job_id"], "job123")

            tool = ImageGenerationPromptHistoryTool()
            tool.profile = SimpleNamespace(memory_user_id="user_a", shared_workspace=tmp)
            tool.current_context = Context(ContextType.TEXT, "把提示词发给我看看")
            tool.current_context["session_id"] = "session-a"
            result = tool.execute({"raw": True})

        self.assertEqual(result.status, "success")
        self.assertIn("Enhanced prompt:", result.result)
        self.assertIn(ENHANCED_PROMPT_MARKER, result.result)

    def test_prompt_history_tool_can_return_raw_stored_prompt_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            expected = "Exact polished prompt from the last Grok job."
            record_prompt_history(
                workspace_root=tmp,
                memory_user_id="user_a",
                session_id="session-a",
                job_id="job123",
                output_path=str(Path(tmp) / "out.png"),
                metadata={
                    "version": "grok-model-rewrite-v2",
                    "enhanced": True,
                    "target": "grok",
                    "use_case": "image_model_rewrite",
                    "original_prompt": "draw",
                    "enhanced_prompt": expected,
                    "library": {},
                    "templates": [],
                },
            )

            tool = ImageGenerationPromptHistoryTool()
            tool.profile = SimpleNamespace(memory_user_id="user_a", shared_workspace=tmp)
            tool.current_context = Context(ContextType.TEXT, "show the exact prompt")
            tool.current_context["session_id"] = "session-a"
            result = tool.execute({"exact_only": True, "raw": True})

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result, expected)

    def test_prompt_history_tool_translates_grok_prompt_with_grok_by_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            record_prompt_history(
                workspace_root=tmp,
                memory_user_id="user_a",
                session_id="session-a",
                job_id="job123",
                output_path=str(Path(tmp) / "out.png"),
                metadata={
                    "version": "grok-model-rewrite-v2",
                    "enhanced": True,
                    "target": "grok",
                    "media_type": "image",
                    "use_case": "image_model_rewrite",
                    "original_prompt": "draw",
                    "enhanced_prompt": "A cinematic portrait with soft light.",
                    "library": {},
                    "templates": [],
                },
            )

            calls = []
            tool = ImageGenerationPromptHistoryTool()
            tool.profile = SimpleNamespace(memory_user_id="user_a", shared_workspace=tmp)
            tool.current_context = Context(ContextType.TEXT, "查看刚才润色后的提示词")
            tool.current_context["session_id"] = "session-a"

            with patch.object(
                prompt_history_module,
                "_translate_prompt_with_grok",
                side_effect=lambda prompt: calls.append(prompt) or "柔和光线下的电影感肖像。",
            ), patch.object(
                prompt_history_module,
                "_translate_prompt_with_attached_model",
                side_effect=AssertionError("Grok records must use Grok translation first"),
            ):
                result = tool.execute({"exact_only": True})

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result, "柔和光线下的电影感肖像。")
        self.assertEqual(calls, ["A cinematic portrait with soft light."])

    def test_prompt_history_tool_translates_direct_grok_record_with_grok(self):
        with tempfile.TemporaryDirectory() as tmp:
            record_prompt_history(
                workspace_root=tmp,
                memory_user_id="user_a",
                session_id="session-a",
                job_id="job123",
                output_path=str(Path(tmp) / "out.png"),
                metadata={
                    "enhanced": False,
                    "target": "grok",
                    "runtime": "grok_direct",
                    "original_prompt": "draw",
                    "enhanced_prompt": "A direct Grok prompt.",
                    "library": {},
                    "templates": [],
                },
            )

            calls = []
            tool = ImageGenerationPromptHistoryTool()
            tool.profile = SimpleNamespace(memory_user_id="user_a", shared_workspace=tmp)
            tool.current_context = Context(ContextType.TEXT, "查看提示词")
            tool.current_context["session_id"] = "session-a"

            with patch.object(
                prompt_history_module,
                "_translate_prompt_with_grok",
                side_effect=lambda prompt: calls.append(prompt) or "Grok 直接提示词。",
            ), patch.object(
                prompt_history_module,
                "_translate_prompt_with_attached_model",
                side_effect=AssertionError("direct Grok records must use Grok translation first"),
            ), patch.object(
                prompt_history_module,
                "_translate_prompt_with_bridge",
                side_effect=AssertionError("direct Grok records must not need bridge translation when Grok succeeds"),
            ):
                result = tool.execute({"exact_only": True})

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result, "Grok 直接提示词。")
        self.assertEqual(calls, ["A direct Grok prompt."])

    def test_prompt_history_tool_translates_non_grok_prompt_with_attached_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            record_prompt_history(
                workspace_root=tmp,
                memory_user_id="user_a",
                session_id="session-a",
                job_id="job123",
                output_path=str(Path(tmp) / "out.png"),
                metadata={
                    "version": "youmind-full-library-v1",
                    "enhanced": True,
                    "target": "gpt",
                    "use_case": "poster",
                    "original_prompt": "draw",
                    "enhanced_prompt": "A clean product poster.",
                    "library": {},
                    "templates": [],
                },
            )

            calls = []
            tool = ImageGenerationPromptHistoryTool()
            tool.model = object()
            tool.profile = SimpleNamespace(memory_user_id="user_a", shared_workspace=tmp)
            tool.current_context = Context(ContextType.TEXT, "查看刚才润色后的提示词")
            tool.current_context["session_id"] = "session-a"

            with patch.object(
                prompt_history_module,
                "_translate_prompt_with_attached_model",
                side_effect=lambda prompt, model: calls.append((prompt, model)) or "干净的产品海报。",
            ):
                result = tool.execute({"exact_only": True})

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result, "干净的产品海报。")
        self.assertEqual(calls, [("A clean product poster.", tool.model)])

    def test_prompt_history_tool_uses_bridge_translation_when_model_is_unavailable(self):
        with tempfile.TemporaryDirectory() as tmp:
            record_prompt_history(
                workspace_root=tmp,
                memory_user_id="user_a",
                session_id="session-a",
                job_id="job123",
                output_path=str(Path(tmp) / "out.png"),
                metadata={
                    "version": "youmind-full-library-v1",
                    "enhanced": True,
                    "target": "gpt",
                    "use_case": "poster",
                    "original_prompt": "draw",
                    "enhanced_prompt": "A clean product poster.",
                    "library": {},
                    "templates": [],
                },
            )

            bridge_calls = []
            tool = ImageGenerationPromptHistoryTool()
            tool.profile = SimpleNamespace(memory_user_id="user_a", shared_workspace=tmp)
            tool.current_context = Context(ContextType.TEXT, "查看提示词")
            tool.current_context["session_id"] = "session-a"

            with patch.object(
                prompt_history_module,
                "_translate_prompt_with_bridge",
                side_effect=lambda prompt: bridge_calls.append(prompt) or "干净的产品海报。",
            ):
                result = tool.execute({"exact_only": True})

        self.assertEqual(result.status, "success")
        self.assertEqual(result.result, "干净的产品海报。")
        self.assertEqual(bridge_calls, ["A clean product poster."])

    def test_redacts_hidden_prompt_from_error_text(self):
        text = (
            "API rejected prompt: "
            f"{ENHANCED_PROMPT_MARKER}\nGeneration context:\n- model: gpt-image-2\nsecret structure"
        )

        redacted = redact_hidden_image_prompt_text(text)

        self.assertIn("API rejected prompt", redacted)
        self.assertIn("hidden enhanced image prompt omitted", redacted)
        self.assertNotIn("Generation context", redacted)


if __name__ == "__main__":
    unittest.main()
