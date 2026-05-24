import json
import unittest
from unittest.mock import patch

from agent.protocol.agent_stream import AgentStreamExecutor


class TestAgentStreamKnowledgeContext(unittest.TestCase):
    def _executor(self):
        return AgentStreamExecutor.__new__(AgentStreamExecutor)

    def test_format_retrieved_knowledge_is_deterministic_and_compact(self):
        executor = self._executor()
        hit_a = {
            "title": "Doc\nTitle",
            "source_path": "kb\\source.md",
            "snippet": "line one\n line two",
            "score": 0.98765,
            "page_start": 3,
            "page_end": 3,
        }
        hit_b = {}
        hit_b["page_end"] = 3
        hit_b["page_start"] = 3
        hit_b["score"] = 0.98765
        hit_b["snippet"] = "line one\n line two"
        hit_b["source_path"] = "kb\\source.md"
        hit_b["title"] = "Doc\nTitle"

        with patch("config.conf", return_value={"knowledge_auto_retrieval_max_chars": 4000}):
            first = executor._format_retrieved_knowledge([hit_a])
            second = executor._format_retrieved_knowledge([hit_b])

        self.assertEqual(first, second)
        record = json.loads(first.split("\n\n")[1])
        self.assertEqual(list(record.keys()), ["index", "page", "score", "snippet", "source", "title"])
        self.assertEqual(record["title"], "Doc Title")
        self.assertEqual(record["snippet"], "line one line two")
        self.assertEqual(record["score"], "0.988")

    def test_format_retrieved_knowledge_preserves_fallbacks_pages_and_scores(self):
        executor = self._executor()
        hits = [
            {
                "path": "knowledge/page.md",
                "text": "body text",
                "page_start": 5,
                "page_end": 7,
                "score": 1,
                "chunk_id": "chunk-1",
            },
            {
                "path": "knowledge/single.md",
                "text": "single body",
                "page_start": 9,
                "page_end": 9,
                "score": "not-number",
            },
        ]

        with patch("config.conf", return_value={"knowledge_auto_retrieval_max_chars": 4000}):
            text = executor._format_retrieved_knowledge(hits)

        first = json.loads(text.split("\n\n")[1])
        second = json.loads(text.split("\n\n")[2])
        self.assertEqual(first["title"], "knowledge/page.md")
        self.assertEqual(first["source"], "knowledge/page.md")
        self.assertEqual(first["page"], "5-7")
        self.assertEqual(first["score"], "1.000")
        self.assertEqual(first["chunk"], "chunk-1")
        self.assertEqual(second["page"], "9")
        self.assertIsNone(second["score"])

    def test_format_retrieved_knowledge_respects_max_chars_without_mid_record_corruption(self):
        executor = self._executor()
        hits = [
            {"title": "A", "source_path": "a.md", "snippet": "short"},
            {"title": "B", "source_path": "b.md", "snippet": "x" * 500},
        ]

        with patch("config.conf", return_value={"knowledge_auto_retrieval_max_chars": 320}):
            text = executor._format_retrieved_knowledge(hits)

        self.assertLessEqual(len(text), 320)
        self.assertTrue(text.endswith("[Retrieved knowledge truncated.]"))
        for line in text.split("\n\n")[1:-1]:
            json.loads(line)

    def test_build_knowledge_context_prefers_backend_auto_inject(self):
        executor = self._executor()
        with (
            patch("config.conf", return_value={
                "knowledge_backend": {"enabled": True, "retrieval": {"auto_inject": True}},
                "knowledge_auto_retrieval": True,
            }),
            patch.object(executor, "_retrieve_backend_knowledge", return_value=[{"snippet": "backend"}]) as backend,
            patch.object(executor, "_retrieve_markdown_knowledge", return_value=[{"snippet": "markdown"}]) as markdown,
            patch.object(executor, "_format_retrieved_knowledge", return_value="formatted"),
        ):
            self.assertEqual(executor._build_knowledge_context_text("q"), "formatted")

        backend.assert_called_once()
        markdown.assert_not_called()

    def test_build_knowledge_context_falls_back_to_markdown_when_backend_auto_inject_disabled(self):
        executor = self._executor()
        with (
            patch("config.conf", return_value={
                "knowledge_backend": {"enabled": True, "retrieval": {"auto_inject": False}},
                "knowledge_auto_retrieval": True,
            }),
            patch.object(executor, "_retrieve_backend_knowledge", return_value=[{"snippet": "backend"}]) as backend,
            patch.object(executor, "_retrieve_markdown_knowledge", return_value=[{"snippet": "markdown"}]) as markdown,
            patch.object(executor, "_format_retrieved_knowledge", return_value="formatted"),
        ):
            self.assertEqual(executor._build_knowledge_context_text("q"), "formatted")

        backend.assert_not_called()
        markdown.assert_called_once()

    def test_build_knowledge_context_returns_empty_when_retrieval_disabled(self):
        executor = self._executor()
        with (
            patch("config.conf", return_value={
                "knowledge_backend": {"enabled": False},
                "knowledge_auto_retrieval": False,
            }),
            patch.object(executor, "_retrieve_backend_knowledge") as backend,
            patch.object(executor, "_retrieve_markdown_knowledge") as markdown,
        ):
            self.assertEqual(executor._build_knowledge_context_text("q"), "")

        backend.assert_not_called()
        markdown.assert_not_called()


if __name__ == "__main__":
    unittest.main()
