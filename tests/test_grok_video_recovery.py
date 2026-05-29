# encoding:utf-8

import json
import time

from agent.tools.video_generation.job_manager import (
    JOB_STATE_FILE,
    GrokVideoGenerationJobManager,
)


def test_grok_video_recovery_skips_delivery_failed_terminal_jobs(tmp_path):
    output_dir = tmp_path / "users" / "user" / "files" / "grok-video-generation" / "job-1"
    output_dir.mkdir(parents=True)
    video_path = output_dir / "result.mp4"
    video_path.write_bytes(b"\x00\x00\x00\x18ftypmp4")
    state_path = output_dir / JOB_STATE_FILE
    state_path.write_text(
        json.dumps(
            {
                "job_id": "job-1",
                "actor_id": "actor",
                "memory_user_id": "user",
                "output_dir": str(output_dir),
                "context_snapshot": {
                    "receiver": "receiver",
                    "channel_type": "wecom_bot",
                },
                "status": "delivery_failed",
                "created_at": time.time() - 60,
                "started_at": time.time() - 30,
                "completed_at": time.time() - 10,
                "output_path": str(video_path),
                "error": "video generation completed, but delivery failed",
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    manager = GrokVideoGenerationJobManager(workspace_root=str(tmp_path))
    completion_attempts = []
    manager._send_completion = lambda job: completion_attempts.append(job.job_id) or True

    try:
        assert manager.recover_unfinished_jobs(notify=True) == []
        assert completion_attempts == []
        assert json.loads(state_path.read_text(encoding="utf-8"))["status"] == "delivery_failed"
    finally:
        manager.shutdown(wait=False)
