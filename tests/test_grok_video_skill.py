# encoding:utf-8

import importlib.util
import time
from pathlib import Path
from types import SimpleNamespace

from agent.tools.base_tool import ToolResult
from agent.tools.video_generation.grok_video_generation_task import GrokVideoGenerationTaskTool
from agent.tools.video_generation.job_manager import GrokVideoGenerationJob, GrokVideoGenerationJobManager
from bridge.context import Context, ContextType
from bridge.reply import ReplyType
from channel.image_recognition import ImageRecognitionManager, reset_image_recognition_manager


class FakeJobManager:
    def __init__(self):
        self.submitted = []

    def submit(self, args, context, profile):
        self.submitted.append((args, context, profile))
        return SimpleNamespace(job_id="job-1")

    def queue_position(self, job):
        return 0


def _tool_with_context(content):
    tool = GrokVideoGenerationTaskTool()
    tool.job_manager = FakeJobManager()
    tool.current_context = Context(ContextType.TEXT, content)
    tool.profile = SimpleNamespace(actor_id="actor", memory_user_id="user")
    return tool


def test_grok_video_task_extracts_explicit_image_refs_and_caps_at_seven():
    refs = "\n".join(f"[图片: C:/tmp/{i}.png]" for i in range(9))
    tool = _tool_with_context(refs)

    result: ToolResult = tool.execute({"prompt": "参考上面9张图片生成视频"})

    assert result.status == "success"
    args = tool.job_manager.submitted[0][0]
    assert len(args["image_url"]) == 7
    assert args["image_url"][0] == "C:/tmp/2.png"
    assert args["image_url"][-1] == "C:/tmp/8.png"
    assert "Task ID: job-1" in result.result


def test_grok_video_task_reference_request_without_count_uses_latest_image():
    refs = "\n".join(f"[图片: C:/tmp/{i}.png]" for i in range(3))
    tool = _tool_with_context(refs)

    result: ToolResult = tool.execute({"prompt": "参考上面的图片生成视频"})

    assert result.status == "success"
    args = tool.job_manager.submitted[0][0]
    assert args["image_url"] == "C:/tmp/2.png"


def test_grok_video_task_refs_without_count_use_latest_image():
    refs = "\n".join(f"[图片: C:/tmp/{i}.png]" for i in range(3))
    tool = _tool_with_context(refs)

    result: ToolResult = tool.execute({"prompt": "背景换成火星"})

    assert result.status == "success"
    args = tool.job_manager.submitted[0][0]
    assert args["image_url"] == "C:/tmp/2.png"


def test_grok_video_task_without_context_ref_uses_latest_recent_image(monkeypatch, tmp_path):
    tool = _tool_with_context("生成视频 10s 让镜头推进")
    tool.current_context["session_id"] = "session"
    workspace = tmp_path / "workspace"
    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    image_manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(image_manager)
    monkeypatch.setattr(ImageRecognitionManager, "_recognize_image", lambda self, path: "summary")
    record = image_manager.register_image(
        session_id="session",
        channel_type="web",
        image_path=str(source),
    )

    result: ToolResult = tool.execute({"prompt": "生成视频 10s 让镜头推进"})

    assert result.status == "success"
    args = tool.job_manager.submitted[0][0]
    assert args["image_url"] == record.image_path
    reset_image_recognition_manager(None)


def test_grok_video_task_prefers_original_context_prompt_and_natural_options():
    tool = _tool_with_context("生成视频 720p 10s 原始猫咪动作\n[image: C:/tmp/ref.png]")
    tool.current_context.type = ContextType.VIDEO_CREATE

    result: ToolResult = tool.execute({
        "prompt": "polished cinematic cat commercial",
        "duration": "6s",
        "resolution": "480p",
    })

    assert result.status == "success"
    args = tool.job_manager.submitted[0][0]
    assert args["prompt"] == "生成视频 720p 10s 原始猫咪动作"
    assert args["duration"] == "10s"
    assert args["resolution"] == "720p"
    assert args["image_url"] == "C:/tmp/ref.png"


def test_grok_video_task_text_to_video_opt_out_skips_recent_image(monkeypatch, tmp_path):
    tool = _tool_with_context("文生视频，一只猫在月球奔跑")
    tool.current_context["session_id"] = "session"
    workspace = tmp_path / "workspace"
    source = tmp_path / "ref.png"
    source.write_bytes(b"\x89PNG\r\n\x1a\nimage")
    image_manager = ImageRecognitionManager(workspace_root=str(workspace), max_workers=1)
    reset_image_recognition_manager(image_manager)
    monkeypatch.setattr(ImageRecognitionManager, "_recognize_image", lambda self, path: "summary")
    image_manager.register_image(
        session_id="session",
        channel_type="web",
        image_path=str(source),
    )

    result: ToolResult = tool.execute({"prompt": "文生视频，一只猫在月球奔跑"})

    assert result.status == "success"
    args = tool.job_manager.submitted[0][0]
    assert "image_url" not in args
    reset_image_recognition_manager(None)


def test_grok_video_task_requires_image_for_reference_request():
    tool = _tool_with_context("")

    result = tool.execute({"prompt": "让这张图动起来，生成视频"})

    assert result.status == "error"
    assert "no input image was found" in result.result


def test_grok_video_job_completion_sends_video_reply(tmp_path):
    manager = GrokVideoGenerationJobManager(workspace_root=str(tmp_path))
    sent = []
    manager._send_reply = lambda job, reply, content: sent.append((reply.type, reply.content, content)) or True
    manager._remember_output = lambda job, content: None
    video_path = tmp_path / "result.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp4")
    job = GrokVideoGenerationJob(
        job_id="job-1",
        actor_id="actor",
        memory_user_id="user",
        args={},
        output_dir=str(tmp_path),
        context_snapshot={"receiver": "user", "channel_type": "wecom_bot"},
        output_path=str(video_path),
    )

    assert manager._send_completion(job) is True
    assert sent[0][0] == ReplyType.TEXT
    assert sent[1] == (ReplyType.VIDEO, str(video_path), "")
    assert "prompt" not in manager._job_state_payload(job)


def test_grok_video_script_outputs_json_only(monkeypatch, tmp_path, capsys):
    script = _load_script_module()
    source = tmp_path / "source.mp4"
    source.write_bytes(b"\x00\x00\x00\x18ftypmp4")
    output_dir = tmp_path / "out"
    captured = {}

    class FakeProvider:
        def generate(self, prompt, **kwargs):
            captured.update({"prompt": prompt, **kwargs})
            return str(source)

    monkeypatch.setattr(script, "_route_cowwecom_console_logs_to_stderr", lambda: None)
    monkeypatch.setattr("integrations.hermes_xai.video_gen.XAIVideoGenProvider", lambda: FakeProvider())

    code = script.main([
        "generate.py",
        '{"prompt":"make video","image_url":["C:/a.png","C:/b.png"],"resolution":"480p","output_dir":"' + str(output_dir).replace("\\", "\\\\") + '"}',
    ])
    stdout = capsys.readouterr().out.strip()

    assert code == 0
    assert stdout.startswith('{"videos":')
    assert (output_dir / "result.mp4").read_bytes() == source.read_bytes()
    assert captured.get("image_url") is None
    assert captured["reference_image_urls"] == ["C:/a.png", "C:/b.png"]
    assert captured["resolution"] == "480p"


def test_grok_video_job_manager_preserves_resolution(tmp_path):
    manager = GrokVideoGenerationJobManager(workspace_root=str(tmp_path))
    try:
        cleaned = manager._clean_args({"prompt": "make video", "resolution": "480p", "unknown": "x"})
    finally:
        manager.shutdown(wait=False)

    assert cleaned == {"prompt": "make video", "resolution": "480p"}


def test_grok_video_job_manager_parallelizes_same_actor(tmp_path):
    manager = GrokVideoGenerationJobManager(
        workspace_root=str(tmp_path),
        global_workers=2,
        actor_workers=2,
        task_timeout=5,
        duplicate_window=0,
    )
    manager._send_reply = lambda job, reply, content: True
    manager._remember_output = lambda job, content: None
    events = []
    event_lock = __import__("threading").Lock()

    def fake_generator(job):
        with event_lock:
            events.append(("start", job.job_id, time.time()))
        time.sleep(0.35)
        video_path = Path(job.output_dir) / "result.mp4"
        video_path.write_bytes(b"\x00\x00\x00\x18ftypmp4")
        with event_lock:
            events.append(("end", job.job_id, time.time()))
        return {"videos": [{"url": str(video_path)}]}

    manager._invoke_generator = fake_generator
    context = Context(ContextType.TEXT, "video")
    context["channel_type"] = "web"
    context["receiver"] = "session"
    context["session_id"] = "session"
    profile = SimpleNamespace(actor_id="actor", memory_user_id="user")

    job_a = manager.submit({"prompt": "first", "image_url": str(events)}, context, profile)
    job_b = manager.submit({"prompt": "second", "image_url": str(events)}, context, profile)
    try:
        assert _wait_for_job(job_a) == "succeeded"
        assert _wait_for_job(job_b) == "succeeded"
        starts = [event for event in events if event[0] == "start"]
        ends = [event for event in events if event[0] == "end"]
        assert len(starts) == 2
        assert len(ends) == 2
        assert abs(starts[0][2] - starts[1][2]) < 0.2
        assert max(event[2] for event in starts) < min(event[2] for event in ends)
    finally:
        manager.shutdown(wait=False)


def test_grok_video_job_manager_can_limit_same_actor_parallelism(tmp_path):
    script = tmp_path / "fake_video.py"
    events = tmp_path / "events.txt"
    script.write_text(
        """
import json, os, sys, time
args = json.loads(sys.argv[1])
event_file = args.get("image_url")
output_dir = args["output_dir"]
os.makedirs(output_dir, exist_ok=True)
job_id = os.path.basename(output_dir)
with open(event_file, "a", encoding="utf-8") as handle:
    handle.write(f"start {job_id} {time.time()}\\n")
time.sleep(0.2)
video_path = os.path.join(output_dir, "result.mp4")
with open(video_path, "wb") as handle:
    handle.write(b"\\x00\\x00\\x00\\x18ftypmp4")
with open(event_file, "a", encoding="utf-8") as handle:
    handle.write(f"end {job_id} {time.time()}\\n")
print(json.dumps({"videos": [{"url": video_path}]}))
""",
        encoding="utf-8",
    )
    manager = GrokVideoGenerationJobManager(
        script_path=str(script),
        workspace_root=str(tmp_path),
        global_workers=2,
        actor_workers=1,
        task_timeout=5,
        duplicate_window=0,
    )
    manager._send_reply = lambda job, reply, content: True
    manager._remember_output = lambda job, content: None
    context = Context(ContextType.TEXT, "video")
    context["channel_type"] = "web"
    context["receiver"] = "session"
    context["session_id"] = "session"
    profile = SimpleNamespace(actor_id="actor", memory_user_id="user")

    job_a = manager.submit({"prompt": "first", "image_url": str(events)}, context, profile)
    job_b = manager.submit({"prompt": "second", "image_url": str(events)}, context, profile)
    try:
        assert _wait_for_job(job_a) == "succeeded"
        assert _wait_for_job(job_b) == "succeeded"
        lines = events.read_text(encoding="utf-8").splitlines()
        assert lines[0].startswith("start")
        assert lines[1].startswith("end")
        assert lines[2].startswith("start")
    finally:
        manager.shutdown(wait=False)


def _wait_for_job(job, timeout=5):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if job.status in ("succeeded", "failed", "cancelled", "delivery_failed"):
            return job.status
        time.sleep(0.02)
    raise AssertionError(f"job {job.job_id} did not finish, status={job.status}")


def _load_script_module():
    path = Path("skills/grok-video-generation/scripts/generate.py").resolve()
    spec = importlib.util.spec_from_file_location("grok_video_generation_generate_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module
