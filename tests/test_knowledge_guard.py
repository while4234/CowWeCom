import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.prompt.builder import _build_knowledge_section
from agent.tools.edit.edit import Edit
from agent.tools.write.write import Write


class TestKnowledgeGuard(unittest.TestCase):
    def test_write_blocks_social_bridge_private_details_in_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = Write({"cwd": tmp})

            result = tool.execute(
                {
                    "path": "knowledge/entities/xiao-zhi.md",
                    "content": "小栀是 Rondle 的老婆，Bridge ID 是 bridge_baa3aa881340b649。",
                }
            )

            self.assertEqual(result.status, "error")
            self.assertIn("must be stored in memory", result.result)
            self.assertFalse((Path(tmp) / "knowledge" / "entities" / "xiao-zhi.md").exists())

    def test_write_allows_general_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = Write({"cwd": tmp})

            result = tool.execute(
                {
                    "path": "knowledge/concepts/prompt-cache.md",
                    "content": "# Prompt Cache\n\nPrompt caching reduces repeated prompt processing cost.",
                }
            )

            self.assertEqual(result.status, "success")
            self.assertTrue((Path(tmp) / "knowledge" / "concepts" / "prompt-cache.md").exists())

    def test_edit_blocks_social_bridge_private_details_in_knowledge(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "knowledge" / "index.md"
            path.parent.mkdir(parents=True)
            path.write_text("# Knowledge Index\n", encoding="utf-8")
            tool = Edit({"cwd": tmp})

            result = tool.execute(
                {
                    "path": "knowledge/index.md",
                    "oldText": "",
                    "newText": "- [小栀](entities/xiao-zhi.md) — 可通过微信桥接联系。\n",
                }
            )

            self.assertEqual(result.status, "error")
            self.assertIn("shared knowledge wiki", result.result)
            self.assertEqual(path.read_text(encoding="utf-8"), "# Knowledge Index\n")

    def test_edit_append_creates_missing_memory_file(self):
        class MemoryManager:
            def __init__(self):
                self.dirty = False

            def mark_dirty(self):
                self.dirty = True

        with tempfile.TemporaryDirectory() as tmp:
            memory_manager = MemoryManager()
            tool = Edit({"cwd": tmp, "memory_manager": memory_manager})

            result = tool.execute(
                {
                    "path": "memory/2026-05-24.md",
                    "oldText": "",
                    "newText": "task note\n",
                }
            )

            path = Path(tmp) / "memory" / "2026-05-24.md"
            created_content = path.read_text(encoding="utf-8")

        self.assertEqual(result.status, "success")
        self.assertEqual(created_content, "task note\n")
        self.assertTrue(memory_manager.dirty)

    def test_edit_replace_still_rejects_missing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            tool = Edit({"cwd": tmp})

            result = tool.execute(
                {
                    "path": "memory/2026-05-24.md",
                    "oldText": "old",
                    "newText": "new",
                }
            )

        self.assertEqual(result.status, "error")
        self.assertIn("File not found", result.result)

    def test_knowledge_prompt_declares_private_bridge_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge_dir = Path(tmp) / "knowledge"
            knowledge_dir.mkdir()
            (knowledge_dir / "index.md").write_text("", encoding="utf-8")

            with patch("agent.prompt.builder.conf", return_value={"knowledge_index_in_system_prompt": False}):
                prompt = "\n".join(_build_knowledge_section(tmp, "zh"))

        self.assertIn("重要公开实体", prompt)
        self.assertIn("普通微信用户", prompt)
        self.assertIn("Bridge ID", prompt)
        self.assertIn("关系记忆", prompt)


if __name__ == "__main__":
    unittest.main()
