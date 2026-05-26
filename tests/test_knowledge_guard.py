import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from agent.prompt.builder import _build_knowledge_section
from agent.tools.edit.edit import Edit
from agent.tools.knowledge_guard import (
    KnowledgeWriteContext,
    reset_knowledge_write_context,
    set_knowledge_write_context,
    validate_knowledge_write,
)
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
            self.assertIn("personal knowledge wiki", result.result)
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

    def test_knowledge_prompt_declares_auto_write_boundary(self):
        with tempfile.TemporaryDirectory() as tmp:
            knowledge_dir = Path(tmp) / "knowledge"
            knowledge_dir.mkdir()
            (knowledge_dir / "index.md").write_text("", encoding="utf-8")

            with patch("agent.prompt.builder.conf", return_value={"knowledge_index_in_system_prompt": False}):
                prompt = "\n".join(_build_knowledge_section(tmp, "zh"))

        self.assertIn("协议/规范", prompt)
        self.assertIn("联网搜索", prompt)
        self.assertIn("普通微信用户", prompt)
        self.assertIn("保存到个人知识库", prompt)

    def test_blocks_protocol_analysis_without_explicit_save(self):
        token = set_knowledge_write_context(
            KnowledgeWriteContext(
                user_message="解释 UCIe PHYRETRAIN encoding",
                task_kind="knowledge",
                used_knowledge_query=True,
                evidence_status="ok",
            )
        )
        try:
            ok, error = validate_knowledge_write(
                "knowledge/analysis/ucie-phyretrain.md",
                "UCIe PHYRETRAIN encoding 结论基于 deep evidence。",
            )
        finally:
            reset_knowledge_write_context(token)

        self.assertFalse(ok)
        self.assertIn("protocol/specification", error)

    def test_allows_explicit_protocol_save_when_evidence_is_sufficient(self):
        token = set_knowledge_write_context(
            KnowledgeWriteContext(
                user_message="把刚才结论保存到个人知识库",
                task_kind="knowledge",
                used_knowledge_query=True,
                evidence_status="ok",
            )
        )
        try:
            ok, error = validate_knowledge_write(
                "knowledge/analysis/ucie-phyretrain.md",
                "Source: current answer and UCIe source evidence. UCIe PHYRETRAIN encoding summary.",
            )
        finally:
            reset_knowledge_write_context(token)

        self.assertTrue(ok)
        self.assertEqual(error, "")

    def test_blocks_insufficient_evidence_even_with_save_intent(self):
        token = set_knowledge_write_context(
            KnowledgeWriteContext(
                user_message="保存到个人知识库",
                task_kind="knowledge",
                used_knowledge_query=True,
                evidence_status="insufficient",
            )
        )
        try:
            ok, error = validate_knowledge_write(
                "knowledge/analysis/ucie-uncertain.md",
                "当前证据还不充分，可能需要继续查原文。",
            )
        finally:
            reset_knowledge_write_context(token)

        self.assertFalse(ok)
        self.assertIn("insufficient", error)

    def test_allows_public_web_research_ingest(self):
        token = set_knowledge_write_context(
            KnowledgeWriteContext(
                user_message="搜索某公开技术概念并整理",
                task_kind="default",
                used_web_research=True,
            )
        )
        try:
            ok, error = validate_knowledge_write(
                "knowledge/sources/public-concept.md",
                "> Source: https://example.com/article\n\n公开资料整理。",
            )
        finally:
            reset_knowledge_write_context(token)

        self.assertTrue(ok)
        self.assertEqual(error, "")


if __name__ == "__main__":
    unittest.main()
