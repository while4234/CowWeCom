import base64
import os
import json
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from config import conf
from agent.tools.image_generation.image_generation_task import ImageGenerationTaskTool
from agent.tools.image_generation.job_manager import (
    ImageGenerationJobManager,
    JOB_STATE_FILE,
    RESTART_RECOVERY_ERROR,
)
from bridge.context import Context, ContextType


PNG_B64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="


class FakeChannel:
    def __init__(self):
        self.sent = []

    def send(self, reply, context):
        self.sent.append((reply.type.name, reply.content, context.get("receiver")))


class FailingChannel(FakeChannel):
    def send(self, reply, context):
        super().send(reply, context)
        return False


class CaptureManager:
    def __init__(self):
        self.submitted = []

    def submit(self, args, context, profile):
        self.submitted.append(dict(args))
        return SimpleNamespace(job_id="job123")

    def queue_position(self, job):
        return 0


def write_fake_generator(path: Path, *, fail: bool = False):
    body = f"""
import base64
import json
import os
import sys
import time

args = json.loads(sys.argv[1])
event_file = args.get("image_url")
prompt = args.get("prompt", "")
sleep_s = 0.0
if "sleep:" in prompt:
    sleep_s = float(prompt.split("sleep:", 1)[1].split()[0])
output_dir = args["output_dir"]
os.makedirs(output_dir, exist_ok=True)
job_id = os.path.basename(output_dir)
if event_file:
    with open(event_file, "a", encoding="utf-8") as f:
        f.write(f"start {{job_id}} {{time.time()}}\\n")
time.sleep(sleep_s)
if {str(fail)}:
    print(json.dumps({{"error": "fake generator failed"}}))
    sys.exit(1)
image_path = os.path.join(output_dir, "out.png")
with open(image_path, "wb") as f:
    f.write(base64.b64decode("{PNG_B64}"))
if event_file:
    with open(event_file, "a", encoding="utf-8") as f:
        f.write(f"end {{job_id}} {{time.time()}}\\n")
print(json.dumps({{"images": [{{"url": image_path}}]}}))
"""
    path.write_text(body, encoding="utf-8")


def make_profile(actor_id: str, memory_user_id: str, root: str):
    return SimpleNamespace(
        actor_id=actor_id,
        memory_user_id=memory_user_id,
        conversation_id=actor_id,
        channel_type="weixin",
        shared_workspace=root,
        tool_workspace=os.path.join(root, "users", memory_user_id, "files"),
    )


def make_context(receiver="receiver"):
    context = Context(ContextType.TEXT, "draw")
    context["channel_type"] = "weixin"
    context["receiver"] = receiver
    context["session_id"] = receiver
    context["isgroup"] = False
    context["msg"] = SimpleNamespace(context_token="token")
    return context


def wait_for(job, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.status in ("succeeded", "failed", "cancelled", "delivery_failed"):
            return job.status
        time.sleep(0.02)
    raise AssertionError(f"job {job.job_id} did not finish, status={job.status}")


class TestImageGenerationBackgroundJobs(unittest.TestCase):
    def test_different_actors_run_concurrently(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_generate.py"
            events = Path(tmp) / "events.txt"
            write_fake_generator(script)
            manager = ImageGenerationJobManager(
                script_path=str(script),
                workspace_root=tmp,
                global_workers=2,
                task_timeout=5,
            )
            manager._get_channel = lambda channel_type: FakeChannel()
            start = time.time()
            job_a = manager.submit(
                {"prompt": "sleep:0.35", "image_url": str(events)},
                make_context("a"),
                make_profile("weixin:a", "user_a", tmp),
            )
            job_b = manager.submit(
                {"prompt": "sleep:0.35", "image_url": str(events)},
                make_context("b"),
                make_profile("weixin:b", "user_b", tmp),
            )
            try:
                self.assertEqual(wait_for(job_a), "succeeded")
                self.assertEqual(wait_for(job_b), "succeeded")
                self.assertLess(time.time() - start, 0.65)
            finally:
                manager.shutdown(wait=False)

    def test_same_actor_runs_fifo(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_generate.py"
            events = Path(tmp) / "events.txt"
            write_fake_generator(script)
            channel = FakeChannel()
            manager = ImageGenerationJobManager(
                script_path=str(script),
                workspace_root=tmp,
                global_workers=4,
                task_timeout=5,
            )
            manager._get_channel = lambda channel_type: channel
            profile = make_profile("weixin:a", "user_a", tmp)
            context = make_context("a")
            job_1 = manager.submit({"prompt": "sleep:0.2 first", "image_url": str(events)}, context, profile)
            job_2 = manager.submit({"prompt": "sleep:0.2 second", "image_url": str(events)}, context, profile)
            try:
                self.assertEqual(wait_for(job_1), "succeeded")
                self.assertEqual(wait_for(job_2), "succeeded")
                lines = events.read_text(encoding="utf-8").splitlines()
                starts = [line for line in lines if line.startswith("start")]
                ends = [line for line in lines if line.startswith("end")]
                self.assertEqual(starts[0].split()[1], job_1.job_id)
                self.assertEqual(ends[0].split()[1], job_1.job_id)
                self.assertEqual(starts[1].split()[1], job_2.job_id)
            finally:
                manager.shutdown(wait=False)

    def test_same_actor_duplicate_args_reuses_existing_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_generate.py"
            write_fake_generator(script)
            channel = FakeChannel()
            manager = ImageGenerationJobManager(
                script_path=str(script),
                workspace_root=tmp,
                global_workers=1,
                task_timeout=5,
                duplicate_window=120,
            )
            manager._get_channel = lambda channel_type: channel
            profile = make_profile("weixin:a", "user_a", tmp)
            context = make_context("a")

            job_1 = manager.submit({"prompt": "sleep:0.1", "quality": "medium"}, context, profile)
            job_2 = manager.submit({"prompt": "sleep:0.1", "quality": "medium"}, context, profile)

            try:
                self.assertIs(job_1, job_2)
                self.assertEqual(len(manager._jobs), 1)
                self.assertEqual(wait_for(job_1), "succeeded")
                image_texts = [content for kind, content, _ in channel.sent if kind == "IMAGE_URL"]
                self.assertEqual(len(image_texts), 1)
            finally:
                manager.shutdown(wait=False)

    def test_completed_duplicate_args_start_new_job(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_generate.py"
            write_fake_generator(script)
            channel = FakeChannel()
            manager = ImageGenerationJobManager(
                script_path=str(script),
                workspace_root=tmp,
                global_workers=1,
                task_timeout=5,
                duplicate_window=120,
            )
            manager._get_channel = lambda channel_type: channel
            profile = make_profile("weixin:a", "user_a", tmp)
            context = make_context("a")

            job_1 = manager.submit({"prompt": "sleep:0", "quality": "medium"}, context, profile)
            try:
                self.assertEqual(wait_for(job_1), "succeeded")

                job_2 = manager.submit({"prompt": "sleep:0", "quality": "medium"}, context, profile)

                self.assertIsNot(job_1, job_2)
                self.assertEqual(len(manager._jobs), 2)
                self.assertEqual(wait_for(job_2), "succeeded")
            finally:
                manager.shutdown(wait=False)

    def test_running_channel_manager_is_used_for_background_reply(self):
        with tempfile.TemporaryDirectory() as tmp:
            channel = FakeChannel()
            fake_manager = SimpleNamespace(get_channel=lambda channel_type: channel)
            fake_app = SimpleNamespace(get_channel_manager=lambda: fake_manager)
            manager = ImageGenerationJobManager(workspace_root=tmp, global_workers=1)

            with patch.dict(sys.modules, {"app": fake_app}):
                self.assertIs(manager._get_channel("weixin_user"), channel)

            manager.shutdown(wait=False)

    def test_tool_returns_before_generation_finishes(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_generate.py"
            write_fake_generator(script)
            channel = FakeChannel()
            manager = ImageGenerationJobManager(
                script_path=str(script),
                workspace_root=tmp,
                global_workers=1,
                task_timeout=5,
            )
            manager._get_channel = lambda channel_type: channel
            tool = ImageGenerationTaskTool()
            tool.job_manager = manager
            tool.current_context = make_context("a")
            tool.profile = make_profile("weixin:a", "user_a", tmp)
            start = time.time()
            try:
                result = tool.execute({"prompt": "生成图片 sleep:0.6"})
                self.assertEqual(result.status, "success")
                self.assertLess(time.time() - start, 0.2)
                self.assertIn("Task ID", result.result)
                for job in list(manager._jobs.values()):
                    wait_for(job)
            finally:
                manager.shutdown(wait=True)

    def test_tool_extracts_single_image_ref_from_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CaptureManager()
            tool = ImageGenerationTaskTool()
            tool.job_manager = manager
            tool.current_context = make_context("a")
            tool.current_context.content = "把这张图里的衣服改成红色\n[图片: C:\\tmp\\input.png]"
            tool.profile = make_profile("weixin:a", "user_a", tmp)

            result = tool.execute({"prompt": "把这张图里的衣服改成红色"})

            self.assertEqual(result.status, "success")
            self.assertEqual(manager.submitted[0]["image_url"], "C:\\tmp\\input.png")

    def test_tool_extracts_multiple_image_refs_from_context(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CaptureManager()
            tool = ImageGenerationTaskTool()
            tool.job_manager = manager
            tool.current_context = make_context("a")
            tool.current_context.content = (
                "把这几张图融合成一张合照\n"
                "[图片: C:\\tmp\\a.png]\n"
                "[图片: C:\\tmp\\b.png]"
            )
            tool.profile = make_profile("weixin:a", "user_a", tmp)

            result = tool.execute({"prompt": "把这几张图融合成一张合照"})

            self.assertEqual(result.status, "success")
            self.assertEqual(manager.submitted[0]["image_url"], ["C:\\tmp\\a.png", "C:\\tmp\\b.png"])

    def test_tool_rejects_image_edit_without_input_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CaptureManager()
            tool = ImageGenerationTaskTool()
            tool.job_manager = manager
            tool.current_context = make_context("a")
            tool.current_context.content = "把这张图里的衣服改成红色"
            tool.profile = make_profile("weixin:a", "user_a", tmp)

            result = tool.execute({"prompt": "把这张图里的衣服改成红色"})

            self.assertEqual(result.status, "error")
            self.assertIn("no input image", result.result)
            self.assertEqual(manager.submitted, [])

    def test_tool_rejects_plain_image_question_with_context_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            manager = CaptureManager()
            tool = ImageGenerationTaskTool()
            tool.job_manager = manager
            tool.current_context = make_context("a")
            tool.current_context.content = "这张图是什么\n[图片: C:\\tmp\\input.png]"
            tool.profile = make_profile("weixin:a", "user_a", tmp)

            result = tool.execute({"prompt": "这张图是什么"})

            self.assertEqual(result.status, "error")
            self.assertIn("does not explicitly ask", result.result)
            self.assertEqual(manager.submitted, [])

    def test_output_path_is_inside_user_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_generate.py"
            write_fake_generator(script)
            channel = FakeChannel()
            manager = ImageGenerationJobManager(script_path=str(script), workspace_root=tmp, global_workers=1)
            manager._get_channel = lambda channel_type: channel
            profile = make_profile("weixin:a", "user_a", tmp)
            job = manager.submit({"prompt": "sleep:0"}, make_context("a"), profile)
            try:
                self.assertEqual(wait_for(job), "succeeded")
                expected_root = os.path.join(tmp, "users", "user_a", "files", "image-generation")
                self.assertTrue(os.path.commonpath([job.output_dir, expected_root]) == expected_root)
            finally:
                manager.shutdown(wait=False)

    def test_job_state_is_persisted_until_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_generate.py"
            write_fake_generator(script)
            channel = FakeChannel()
            manager = ImageGenerationJobManager(script_path=str(script), workspace_root=tmp, global_workers=1)
            manager._get_channel = lambda channel_type: channel
            job = manager.submit({"prompt": "sleep:0"}, make_context("a"), make_profile("weixin:a", "user_a", tmp))
            try:
                state_path = Path(job.output_dir) / JOB_STATE_FILE
                self.assertTrue(state_path.exists())
                self.assertNotIn("prompt", json.loads(state_path.read_text(encoding="utf-8")))

                self.assertEqual(wait_for(job), "succeeded")
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["job_id"], job.job_id)
                self.assertEqual(state["status"], "succeeded")
                self.assertTrue(Path(state["output_path"]).exists())
                self.assertIsNotNone(state["completed_at"])
            finally:
                manager.shutdown(wait=False)

    def test_recover_unfinished_job_sends_failure_notice(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "users" / "user_a" / "files" / "image-generation" / "orphanjob"
            output_dir.mkdir(parents=True)
            state_path = output_dir / JOB_STATE_FILE
            state_path.write_text(
                json.dumps({
                    "job_id": "orphanjob",
                    "actor_id": "weixin:a",
                    "memory_user_id": "user_a",
                    "output_dir": str(output_dir),
                    "context_snapshot": {
                        "channel_type": "weixin",
                        "receiver": "a",
                        "isgroup": False,
                        "session_id": "a",
                        "actor_id": "weixin:a",
                        "memory_user_id": "user_a",
                    },
                    "status": "running",
                    "created_at": time.time() - 60,
                }),
                encoding="utf-8",
            )
            channel = FakeChannel()
            manager = ImageGenerationJobManager(workspace_root=tmp, global_workers=1)
            manager._get_channel = lambda channel_type: channel
            try:
                recovered = manager.recover_unfinished_jobs()

                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].status, "failed")
                failure_texts = [content for kind, content, _ in channel.sent if kind == "TEXT"]
                self.assertEqual(len(failure_texts), 1)
                self.assertIn("orphanjob", failure_texts[0])
                self.assertIn(RESTART_RECOVERY_ERROR, failure_texts[0])
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "failed")
                self.assertIn(RESTART_RECOVERY_ERROR, state["error"])
            finally:
                manager.shutdown(wait=False)

    def test_recover_unfinished_job_with_output_resends_image(self):
        with tempfile.TemporaryDirectory() as tmp:
            output_dir = Path(tmp) / "users" / "user_a" / "files" / "image-generation" / "generatedjob"
            output_dir.mkdir(parents=True)
            image_path = output_dir / "out.png"
            image_path.write_bytes(base64.b64decode(PNG_B64))
            state_path = output_dir / JOB_STATE_FILE
            state_path.write_text(
                json.dumps({
                    "job_id": "generatedjob",
                    "actor_id": "weixin:a",
                    "memory_user_id": "user_a",
                    "output_dir": str(output_dir),
                    "output_path": str(image_path),
                    "context_snapshot": {
                        "channel_type": "weixin",
                        "receiver": "a",
                        "isgroup": False,
                        "session_id": "a",
                        "actor_id": "weixin:a",
                        "memory_user_id": "user_a",
                    },
                    "status": "running",
                    "created_at": time.time() - 60,
                }),
                encoding="utf-8",
            )
            channel = FakeChannel()
            manager = ImageGenerationJobManager(workspace_root=tmp, global_workers=1)
            manager._get_channel = lambda channel_type: channel
            try:
                recovered = manager.recover_unfinished_jobs()

                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].status, "succeeded")
                kinds = [kind for kind, _, _ in channel.sent]
                self.assertIn("TEXT", kinds)
                self.assertIn("IMAGE_URL", kinds)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "succeeded")
                self.assertEqual(Path(state["output_path"]), image_path)
            finally:
                manager.shutdown(wait=False)

    def test_delivery_failed_job_is_persisted_for_recovery(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_generate.py"
            write_fake_generator(script)
            manager = ImageGenerationJobManager(script_path=str(script), workspace_root=tmp, global_workers=1)
            manager._get_channel = lambda channel_type: FailingChannel()
            job = manager.submit({"prompt": "sleep:0"}, make_context("a"), make_profile("weixin:a", "user_a", tmp))
            try:
                self.assertEqual(wait_for(job), "delivery_failed")
                state_path = Path(job.output_dir) / JOB_STATE_FILE
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "delivery_failed")
                self.assertTrue(Path(state["output_path"]).exists())
            finally:
                manager.shutdown(wait=False)

            channel = FakeChannel()
            recovery_manager = ImageGenerationJobManager(workspace_root=tmp, global_workers=1)
            recovery_manager._get_channel = lambda channel_type: channel
            try:
                recovered = recovery_manager.recover_unfinished_jobs()

                self.assertEqual(len(recovered), 1)
                self.assertEqual(recovered[0].status, "succeeded")
                kinds = [kind for kind, _, _ in channel.sent]
                self.assertIn("TEXT", kinds)
                self.assertIn("IMAGE_URL", kinds)
                state = json.loads(state_path.read_text(encoding="utf-8"))
                self.assertEqual(state["status"], "succeeded")
            finally:
                recovery_manager.shutdown(wait=False)

    def test_manager_defaults_to_codex_auth_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_generate.py"
            write_fake_generator(script)
            manager = ImageGenerationJobManager(script_path=str(script), workspace_root=tmp, global_workers=1)
            manager._get_channel = lambda channel_type: FakeChannel()
            job = manager.submit(
                {"prompt": "sleep:0"},
                make_context("a"),
                make_profile("weixin:a", "user_a", tmp),
            )
            try:
                self.assertEqual(job.args["runtime"], "codex_auth")
                self.assertEqual(wait_for(job), "succeeded")
            finally:
                manager.shutdown(wait=False)

    def test_manager_does_not_inject_broker_for_codex_auth_runtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_dir = Path(tmp) / "scripts"
            script_dir.mkdir()
            script = script_dir / "fake_generate.py"
            env_file = Path(tmp) / "broker_env.json"
            script.write_text(
                "\n".join([
                    "import base64, json, os, sys",
                    "args = json.loads(sys.argv[1])",
                    f"open({str(env_file)!r}, 'w', encoding='utf-8').write(os.environ.get('SKILL_IMAGE_GENERATION_BROKER_COMMAND_JSON', ''))",
                    "os.makedirs(args['output_dir'], exist_ok=True)",
                    "out = os.path.join(args['output_dir'], 'out.png')",
                    f"open(out, 'wb').write(base64.b64decode({PNG_B64!r}))",
                    "print(json.dumps({'images': [{'url': out}]}))",
                ]),
                encoding="utf-8",
            )
            manager = ImageGenerationJobManager(script_path=str(script), workspace_root=tmp, global_workers=1)
            manager._get_channel = lambda channel_type: FakeChannel()
            job = manager.submit({"prompt": "sleep:0"}, make_context("a"), make_profile("weixin:a", "user_a", tmp))
            try:
                self.assertEqual(wait_for(job), "succeeded")
                self.assertEqual(env_file.read_text(encoding="utf-8"), "")
            finally:
                manager.shutdown(wait=False)

    def test_manager_injects_codex_auth_file_from_skill_config(self):
        with tempfile.TemporaryDirectory() as tmp:
            script_dir = Path(tmp) / "scripts"
            script_dir.mkdir()
            script = script_dir / "fake_generate.py"
            env_file = Path(tmp) / "codex_auth_file.txt"
            auth_file = Path(tmp) / "auth.json"
            auth_file.write_text("{}", encoding="utf-8")
            script.write_text(
                "\n".join([
                    "import base64, json, os, sys",
                    "args = json.loads(sys.argv[1])",
                    f"open({str(env_file)!r}, 'w', encoding='utf-8').write(os.environ.get('CODEX_AUTH_FILE', ''))",
                    "os.makedirs(args['output_dir'], exist_ok=True)",
                    "out = os.path.join(args['output_dir'], 'out.png')",
                    f"open(out, 'wb').write(base64.b64decode({PNG_B64!r}))",
                    "print(json.dumps({'images': [{'url': out}]}))",
                ]),
                encoding="utf-8",
            )
            manager = ImageGenerationJobManager(script_path=str(script), workspace_root=tmp, global_workers=1)
            manager._get_channel = lambda channel_type: FakeChannel()
            original_skill = conf().get("skill", {})
            conf()["skill"] = {"image-generation": {"codex_auth_file": str(auth_file)}}
            try:
                job = manager.submit({"prompt": "sleep:0"}, make_context("a"), make_profile("weixin:a", "user_a", tmp))
                self.assertEqual(wait_for(job), "succeeded")
                configured = env_file.read_text(encoding="utf-8")
                self.assertTrue(os.path.samefile(configured, auth_file))
            finally:
                conf()["skill"] = original_skill
                manager.shutdown(wait=False)

    def test_failed_generation_sends_failure_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "fake_generate.py"
            write_fake_generator(script, fail=True)
            channel = FakeChannel()
            manager = ImageGenerationJobManager(script_path=str(script), workspace_root=tmp, global_workers=1)
            manager._get_channel = lambda channel_type: channel
            job = manager.submit(
                {"prompt": "sleep:0"},
                make_context("a"),
                make_profile("weixin:a", "user_a", tmp),
            )
            try:
                self.assertEqual(wait_for(job), "failed")
                failure_texts = [content for kind, content, _ in channel.sent if kind == "TEXT"]
                self.assertEqual(len(failure_texts), 1)
                self.assertIn("不会用相同参数反复重试", failure_texts[0])
            finally:
                manager.shutdown(wait=False)


if __name__ == "__main__":
    unittest.main()
