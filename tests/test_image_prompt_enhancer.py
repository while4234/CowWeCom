import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

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
REFERENCE_ROOT = PROJECT_ROOT / "skills" / "image-generation" / "references" / "nano-banana-pro"


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


class TestImagePromptEnhancer(unittest.TestCase):
    def test_full_prompt_library_reference_snapshot_is_present(self):
        manifest = json.loads((REFERENCE_ROOT / "manifest.json").read_text(encoding="utf-8"))

        self.assertGreaterEqual(manifest["totalPrompts"], 10000)
        self.assertTrue((REFERENCE_ROOT / "profile-avatar.json").exists())
        self.assertTrue((REFERENCE_ROOT / "social-media-post.json").exists())

    def test_grok_defaults_to_portrait_prompt_shape(self):
        with tempfile.TemporaryDirectory() as tmp:
            library = Path(tmp)
            write_fixture_library(library)

            result = enhance_image_prompt(
                "grok 帮我生成一张高级感人物写真",
                target="grok",
                model="grok-imagine-image",
                library_dir=str(library),
            )

        self.assertTrue(result["enhanced"])
        self.assertEqual(result["use_case"], "portrait")
        self.assertIn(ENHANCED_PROMPT_MARKER, result["enhanced_prompt"])
        self.assertIn("Grok Imagine portrait prompt", result["enhanced_prompt"])
        self.assertEqual(result["templates"][0]["category_slug"], "profile-avatar")

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
            result = tool.execute({})

        self.assertEqual(result.status, "success")
        self.assertIn("Enhanced prompt:", result.result)
        self.assertIn(ENHANCED_PROMPT_MARKER, result.result)

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
