import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from agent.protocol.models import LLMRequest
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

    def test_format_retrieved_knowledge_preserves_deep_evidence_metadata(self):
        executor = self._executor()
        hits = [
            {
                "answer_policy": "separate direct support and inference",
                "chunk_id": "chunk-1",
                "confidence": 0.9,
                "coverage_terms": ["TVLD_L"],
                "deep_query": True,
                "document_id": "doc-1",
                "hit": True,
                "missing_terms": ["TRDVLD_L"],
                "ordinal": 4,
                "page_start": 117,
                "page_end": 118,
                "score": 0.95,
                "section": "MBINIT.REPAIRVAL",
                "source_path": "ucie.pdf",
                "source_span_ids": ["span-1"],
                "snippet": "Step 12 repeats Step 1 through Step 4.",
                "status": "ok",
                "title": "UCIe",
                "truncated": False,
            }
        ]

        with patch("config.conf", return_value={"knowledge_auto_retrieval_max_chars": 4000}):
            text = executor._format_retrieved_knowledge(hits)

        record = json.loads(text.split("\n\n")[1])
        self.assertTrue(record["deep_query"])
        self.assertEqual(record["chunk"], "chunk-1")
        self.assertEqual(record["document_id"], "doc-1")
        self.assertEqual(record["section"], "MBINIT.REPAIRVAL")
        self.assertEqual(record["source_span_ids"], ["span-1"])
        self.assertEqual(record["status"], "ok")

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
            self.assertEqual(executor._build_knowledge_context_text("UCIe q"), "formatted")

        backend.assert_called_once()
        markdown.assert_not_called()

    def test_retrieve_backend_knowledge_uses_deep_query_for_protocol_questions(self):
        executor = self._executor()
        service = type(
            "Service",
            (),
            {
                "deep_query": lambda self, *args, **kwargs: {
                    "status": "ok",
                    "answer_policy": "policy",
                    "coverage_terms": ["protocol"],
                    "missing_terms": [],
                    "confidence": 1.0,
                    "evidence_blocks": [
                        {
                            "chunk_id": "chunk-1",
                            "document_id": "doc-1",
                            "hit": True,
                            "ordinal": 1,
                            "page_start": 1,
                            "page_end": 1,
                            "score": 1.0,
                            "section": "Section",
                            "source": "spec.pdf",
                            "source_span_ids": ["span-1"],
                            "text": "protocol evidence",
                            "title": "Spec",
                            "truncated": False,
                        }
                    ],
                },
                "search": lambda self, *args, **kwargs: [],
            },
        )()

        with patch("agent.knowledge.backend.get_backend_service", return_value=service):
            hits = executor._retrieve_backend_knowledge(
                "请确认协议 Step 1 through Step 4",
                {"retrieval": {"deep_query_enabled": True, "deep_top_k": 3}},
            )

        self.assertEqual(hits[0]["snippet"], "protocol evidence")
        self.assertTrue(hits[0]["deep_query"])
        self.assertEqual(hits[0]["source_span_ids"], ["span-1"])

    def test_retrieve_backend_knowledge_can_disable_deep_query(self):
        executor = self._executor()
        service = type(
            "Service",
            (),
            {
                "deep_query": lambda self, *args, **kwargs: (_ for _ in ()).throw(AssertionError("unexpected")),
                "search": lambda self, *args, **kwargs: [{"title": "Search", "snippet": "fallback"}],
            },
        )()

        with patch("agent.knowledge.backend.get_backend_service", return_value=service):
            hits = executor._retrieve_backend_knowledge(
                "请确认协议 Step 1",
                {"retrieval": {"deep_query_enabled": False, "final_top_k": 2}},
            )

        self.assertEqual(hits[0]["snippet"], "fallback")

    def test_deep_knowledge_trigger_requires_technical_context_for_generic_questions(self):
        executor = self._executor()

        self.assertFalse(executor._should_use_deep_knowledge("今天是否要带伞"))
        self.assertTrue(executor._should_use_deep_knowledge("请确认 UCIe 协议 Step 12 的原文依据"))

    def test_plain_work_progress_snapshot_skips_knowledge_auto_injection(self):
        executor = self._executor()
        prompt = "Feature list\u5b8c\u621090% tc_list\u5b8c\u621030%"

        self.assertFalse(executor._should_use_deep_knowledge(prompt))
        self.assertFalse(executor._looks_like_protocol_question(prompt))
        with (
            patch("config.conf", return_value={
                "knowledge_backend": {"enabled": True, "retrieval": {"auto_inject": True}},
                "knowledge_auto_retrieval": True,
            }),
            patch.object(executor, "_retrieve_backend_knowledge", side_effect=AssertionError("unexpected")),
            patch.object(executor, "_retrieve_markdown_knowledge", side_effect=AssertionError("unexpected")),
        ):
            self.assertEqual(executor._build_knowledge_context_text(prompt), "")

    def test_latest_context_focus_marks_recent_exchange_for_any_reply(self):
        executor = self._executor()
        executor.messages = [
            {
                "role": "user",
                "content": [{"type": "text", "text": "UCIe byte map 怎么理解？"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "UCIe 旧答案和证据。"}],
            },
            {
                "role": "user",
                "content": [{"type": "text", "text": "Feature list完成90% tc_list完成30"}],
            },
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "进度已记录，需要补充收获吗？"}],
            },
        ]

        text = executor._build_latest_context_focus_text("没有")

        self.assertIn("latest assistant prompt", text.lower())
        self.assertIn("进度已记录，需要补充收获吗", text)
        self.assertIn("Feature list完成90%", text)
        self.assertIn("do not continue", text)
        self.assertNotIn("UCIe 旧答案", text)

    def test_short_contextual_reply_request_history_keeps_recent_turns(self):
        executor = self._executor()
        executor._current_user_message = "没有"
        executor.messages = [
            {"role": "user", "content": [{"type": "text", "text": "UCIe 问题"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "UCIe 旧答案"}]},
            {"role": "user", "content": [{"type": "text", "text": "Feature list完成90% tc_list完成30"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "进度已记录，需要补充收获吗？"}]},
            {"role": "user", "content": [{"type": "text", "text": "没有"}]},
        ]
        executor._request_runtime_context = ""

        with patch("config.conf", return_value={"short_contextual_reply_keep_turns": 2}):
            messages = executor._prepare_messages()

        request_text = "\n".join(
            block["text"]
            for message in messages
            for block in message.get("content", [])
            if isinstance(block, dict) and block.get("type") == "text"
        )
        self.assertNotIn("UCIe 旧答案", request_text)
        self.assertIn("Feature list完成90%", request_text)
        self.assertIn("进度已记录，需要补充收获吗", request_text)
        self.assertTrue(request_text.rstrip().endswith("没有"))

    def test_short_contextual_reply_skips_knowledge_auto_injection(self):
        executor = self._executor()
        executor.messages = [
            {"role": "user", "content": [{"type": "text", "text": "UCIe byte map 怎么理解？"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "UCIe 旧答案。"}]},
            {"role": "user", "content": [{"type": "text", "text": "Feature list完成90% tc_list完成30"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "进度已记录，需要补充收获吗？"}]},
        ]

        with (
            patch("config.conf", return_value={
                "knowledge_backend": {"enabled": True, "retrieval": {"auto_inject": True}},
                "knowledge_auto_retrieval": True,
            }),
            patch.object(executor, "_retrieve_backend_knowledge", side_effect=AssertionError("unexpected")),
            patch.object(executor, "_retrieve_markdown_knowledge", side_effect=AssertionError("unexpected")),
        ):
            self.assertEqual(executor._build_knowledge_context_text("没有"), "")

    def test_knowledge_followup_uses_latest_knowledge_context_query(self):
        executor = self._executor()
        executor.messages = [
            {"role": "user", "content": [{"type": "text", "text": "UCIe byte map 怎么理解？"}]},
            {"role": "assistant", "content": [{"type": "text", "text": "UCIe 旧答案。"}]},
        ]
        hit = {"title": "UCIe", "source_path": "ucie.pdf", "snippet": "byte map detail", "score": 0.9}
        captured = {}

        def fake_retrieve(query, backend_conf):
            captured["query"] = query
            return [hit]

        with (
            patch("config.conf", return_value={
                "knowledge_backend": {"enabled": True, "retrieval": {"auto_inject": True}},
            }),
            patch.object(executor, "_retrieve_backend_knowledge", side_effect=fake_retrieve),
        ):
            text = executor._build_knowledge_context_text("这个字段呢")

        self.assertIn("UCIe byte map", captured["query"])
        self.assertIn("Follow-up: 这个字段呢", captured["query"])
        self.assertIn("byte map detail", text)

    def test_knowledge_reasoning_effort_is_xhigh_locked(self):
        captured = {}

        class FakeModel:
            model = "fake-model"
            is_group = False

            def call_stream(self, request: LLMRequest):
                captured["reasoning_effort"] = getattr(request, "reasoning_effort", None)
                captured["reasoning_effort_locked"] = getattr(request, "reasoning_effort_locked", None)
                yield {"choices": [{"delta": {"content": "answer"}}]}

        executor = AgentStreamExecutor(
            agent=SimpleNamespace(
                runtime_info={},
                _get_model_context_window=lambda: 200000,
                _estimate_message_tokens=lambda _message: 1,
            ),
            model=FakeModel(),
            system_prompt="system",
            tools=[],
            max_turns=1,
            messages=[],
            max_context_turns=20,
        )

        with patch("config.conf", return_value={
            "knowledge_backend": {"enabled": False},
            "knowledge_auto_retrieval": False,
            "enable_thinking": False,
            "cowagent_self_evolution_skip_medium_context": True,
            "reasoning_effort_policy_enabled": False,
            "reasoning_effort_policy_audit_enabled": False,
        }):
            response = executor.run_stream("UCIe PHYRETRAIN encoding 表格依据是什么？")

        self.assertIn("answer", response)
        self.assertEqual(captured["reasoning_effort"], "xhigh")
        self.assertTrue(captured["reasoning_effort_locked"])

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
            self.assertEqual(executor._build_knowledge_context_text("UCIe q"), "formatted")

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
