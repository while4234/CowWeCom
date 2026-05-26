import json
import os
import tempfile
import unittest
import importlib.util
from pathlib import Path
from types import SimpleNamespace

from agent.protocol.models import LLMRequest
from common import project_optimizer_evidence
from config import conf


def _load_query_script():
    path = Path(__file__).resolve().parents[1] / "skills" / "cowwechat-project-optimizer" / "scripts" / "query_incremental_calls.py"
    spec = importlib.util.spec_from_file_location("query_incremental_calls", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


class ProjectOptimizerEvidenceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.old_conf = dict(conf())
        conf().clear()
        conf().update({
            "agent_workspace": self.tmp.name,
            "project_optimizer_evidence_enabled": True,
            "project_optimizer_raw_capture_enabled": True,
            "project_optimizer_preserve_temp_scripts": True,
            "project_optimizer_delete_raw_after_run": True,
        })

    def tearDown(self):
        conf().clear()
        conf().update(self.old_conf)
        self.tmp.cleanup()

    def _read_events(self):
        rows = []
        for path in project_optimizer_evidence.event_files():
            with path.open("r", encoding="utf-8") as fh:
                rows.extend(json.loads(line) for line in fh if line.strip())
        return rows

    def _read_raw(self):
        rows = []
        for path in project_optimizer_evidence.raw_files():
            with path.open("r", encoding="utf-8") as fh:
                rows.extend(json.loads(line) for line in fh if line.strip())
        return rows

    def test_records_sanitized_event_and_local_raw_request_with_secret_redaction(self):
        request = LLMRequest(
            messages=[
                {"role": "user", "content": "please analyze my private input"},
                {"role": "assistant", "content": [{"type": "thinking", "thinking": "hidden reasoning"}]},
            ],
            tools=[{"name": "read", "api_key": "local-secret-placeholder", "input_schema": {"type": "object"}}],
            system="system",
            model="gpt-5.5",
            stream=True,
            cache_shape_metadata={"request_kind": "normal", "self_evolution_context_chars": 12},
        )

        request_id = project_optimizer_evidence.record_llm_request(
            request,
            metadata={"session_id": "session-secret", "user_id": "user-secret", "channel_type": "weixin"},
        )

        events = self._read_events()
        raw = self._read_raw()
        serialized_events = json.dumps(events, ensure_ascii=False)
        serialized_raw = json.dumps(raw, ensure_ascii=False)

        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["event_type"], "llm_request")
        self.assertEqual(events[0]["event_id"], request_id)
        self.assertNotIn("please analyze my private input", serialized_events)
        self.assertNotIn("session-secret", serialized_events)
        self.assertNotIn("user-secret", serialized_events)

        self.assertIn("please analyze my private input", serialized_raw)
        self.assertNotIn("local-secret-placeholder", serialized_raw)
        self.assertNotIn("hidden reasoning", serialized_raw)
        self.assertIn("[REDACTED_REASONING]", serialized_raw)

    def test_consuming_raw_cache_deletes_only_raw_records_after_report_use(self):
        project_optimizer_evidence.record_agent_task_start(
            "raw input to consume",
            model_adapter=SimpleNamespace(channel_type="wecom_bot", session_id="s", user_id="u"),
        )
        self.assertTrue(project_optimizer_evidence.raw_files())

        result = project_optimizer_evidence.consume_raw_input_cache(reason="test")

        self.assertEqual(result["deleted_record_count"], 1)
        self.assertFalse(project_optimizer_evidence.raw_files())
        events = self._read_events()
        self.assertEqual(events[-1]["event_type"], "raw_input_cache_consumed")

    def test_archives_only_temporary_scripts(self):
        tmp_script = Path(self.tmp.name) / "workspace" / "scratch.py"
        real_script = Path(self.tmp.name) / "src" / "real.py"
        tmp_script.parent.mkdir(parents=True)
        real_script.parent.mkdir(parents=True)
        tmp_script.write_text("print('tmp')\n", encoding="utf-8")
        real_script.write_text("print('real')\n", encoding="utf-8")

        archived = project_optimizer_evidence.archive_temp_script(
            str(tmp_script),
            cwd=self.tmp.name,
            source="test",
            visible_path="workspace/scratch.py",
        )
        skipped = project_optimizer_evidence.archive_temp_script(
            str(real_script),
            cwd=self.tmp.name,
            source="test",
            visible_path="src/real.py",
        )

        self.assertIsNotNone(archived)
        self.assertIsNone(skipped)
        manifest = project_optimizer_evidence.temp_script_manifest_path()
        self.assertTrue(manifest.exists())
        manifest_text = manifest.read_text(encoding="utf-8")
        self.assertIn("scratch.py", manifest_text)
        self.assertNotIn("real.py", manifest_text)

    def test_query_incremental_calls_uses_local_state_without_reading_raw_cache(self):
        query = _load_query_script()
        workspace = Path(self.tmp.name)
        usage_path = workspace / "data" / "llm_cache_usage.jsonl"
        usage_path.parent.mkdir(parents=True, exist_ok=True)
        usage_path.write_text("{}\n{}\n{}\n", encoding="utf-8")
        state_path = workspace / "data" / "project-optimizer" / "codex_optimizer_state.json"

        status = query.build_status(workspace, state_path, threshold=3)

        self.assertTrue(status["due"])
        self.assertEqual(status["incremental_calls"], 3)

        query.mark_optimized(state_path, status, report_path="local-report.md")
        status = query.build_status(workspace, state_path, threshold=3)

        self.assertFalse(status["due"])
        self.assertEqual(status["incremental_calls"], 0)
        self.assertEqual(status["last_report_path"], "local-report.md")


if __name__ == "__main__":
    unittest.main()
