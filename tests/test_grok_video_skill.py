# encoding:utf-8

import importlib.util
from pathlib import Path
from types import SimpleNamespace

from agent.tools.base_tool import ToolResult
from agent.tools.video_generation.grok_video_generation_task import GrokVideoGenerationTaskTool
from agent.tools.video_generation.job_manager import GrokVideoGenerationJob, GrokVideoGenerationJobManager
from bridge.context import Context, ContextType
from bridge.reply import ReplyType


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


def test_grok_video_task_extracts_image_refs_and_caps_at_seven():
    refs = "\n".join(f"[图片: C:/tmp/{i}.png]" for i in range(9))
    tool = _tool_with_context(refs)

    result: ToolResult = tool.execute({"prompt": "参考上面图片生成视频"})

    assert result.status == "success"
    args = tool.job_manager.submitted[0][0]
    assert len(args["image_url"]) == 7
    assert args["image_url"][0] == "C:/tmp/0.png"
    assert "Task ID: job-1" in result.result


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


def _load_script_module():
    path = Path("skills/grok-video-generation/scripts/generate.py").resolve()
    spec = importlib.util.spec_from_file_location("grok_video_generation_generate_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module
