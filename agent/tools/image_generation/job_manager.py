from __future__ import annotations

import json
import os
import queue
import subprocess
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict, Optional

from bridge.context import Context, ContextType
from bridge.reply import Reply, ReplyType
from common.log import logger
from common.utils import expand_path
from config import conf


JobStatus = str
DEFAULT_IMAGE_GENERATION_RUNTIME = "codex_auth"
BROKER_RUNTIMES = {
    "broker",
    "codex_broker",
    "codex-broker",
    "external_broker",
    "external-broker",
    "local_broker",
    "local-broker",
}


@dataclass
class ImageGenerationJob:
    job_id: str
    actor_id: str
    memory_user_id: str
    args: Dict[str, Any]
    output_dir: str
    context_snapshot: Dict[str, Any]
    status: JobStatus = "queued"
    created_at: float = field(default_factory=time.time)
    started_at: Optional[float] = None
    completed_at: Optional[float] = None
    output_path: Optional[str] = None
    error: Optional[str] = None


class ImageGenerationJobManager:
    """Runs image generation in per-user FIFO queues.

    One worker drains one actor queue at a time, while the shared executor limits
    the number of different actors that can generate concurrently.
    """

    def __init__(
        self,
        agent_bridge=None,
        *,
        script_path: Optional[str] = None,
        workspace_root: Optional[str] = None,
        global_workers: Optional[int] = None,
        task_timeout: Optional[int] = None,
        duplicate_window: Optional[int] = None,
    ):
        self.agent_bridge = agent_bridge
        self.workspace_root = os.path.abspath(
            expand_path(workspace_root or conf().get("agent_workspace", "~/cow"))
        )
        self.script_path = os.path.abspath(
            expand_path(
                script_path
                or os.path.join(
                    self.workspace_root,
                    "skills",
                    "image-generation",
                    "scripts",
                    "generate.py",
                )
            )
        )
        self.global_workers = max(
            int(global_workers or conf().get("image_generation_global_workers", 4) or 4),
            1,
        )
        self.task_timeout = max(
            int(task_timeout or conf().get("image_generation_task_timeout", 600) or 600),
            1,
        )
        self.duplicate_window = max(
            int(
                duplicate_window
                if duplicate_window is not None
                else conf().get("image_generation_duplicate_window", 120) or 120
            ),
            0,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.global_workers,
            thread_name_prefix="imagegen",
        )
        self._lock = threading.RLock()
        self._queues: Dict[str, "queue.Queue[ImageGenerationJob]"] = {}
        self._active_workers: set[str] = set()
        self._jobs: Dict[str, ImageGenerationJob] = {}
        self._recent_submissions: Dict[tuple[str, str], str] = {}

    def submit(self, args: Dict[str, Any], context: Any, profile: Any) -> ImageGenerationJob:
        prompt = str((args or {}).get("prompt", "")).strip()
        if not prompt:
            raise ValueError("prompt is required")
        if profile is None:
            raise ValueError("missing user profile")

        actor_id = str(getattr(profile, "actor_id", "") or self._context_get(context, "actor_id", "unknown"))
        memory_user_id = str(
            getattr(profile, "memory_user_id", "")
            or self._context_get(context, "memory_user_id", "unknown")
        )
        job_args = self._clean_args(args)
        dedupe_key = (actor_id, self._args_signature(job_args))
        now = time.time()

        with self._lock:
            if self.duplicate_window:
                self._prune_recent_submissions(now)
                existing = self._get_recent_duplicate(dedupe_key, now)
                if existing:
                    logger.info(
                        f"[ImageGenerationJobManager] duplicate image task ignored: "
                        f"actor={actor_id} existing_job={existing.job_id}"
                    )
                    return existing

            job_id = uuid.uuid4().hex[:12]
            output_dir = self._build_output_dir(profile, memory_user_id, job_id)
            os.makedirs(output_dir, exist_ok=True)

            job_args = dict(job_args)
            job_args["output_dir"] = output_dir
            job = ImageGenerationJob(
                job_id=job_id,
                actor_id=actor_id,
                memory_user_id=memory_user_id,
                args=job_args,
                output_dir=output_dir,
                context_snapshot=self._snapshot_context(context, profile),
            )
            q = self._queues.setdefault(actor_id, queue.Queue())
            q.put(job)
            self._jobs[job_id] = job
            if self.duplicate_window:
                self._recent_submissions[dedupe_key] = job_id
            if actor_id not in self._active_workers:
                self._active_workers.add(actor_id)
                self._executor.submit(self._run_actor_queue, actor_id)
        return job

    def _get_recent_duplicate(self, dedupe_key: tuple[str, str], now: float) -> Optional[ImageGenerationJob]:
        job_id = self._recent_submissions.get(dedupe_key)
        if not job_id:
            return None
        job = self._jobs.get(job_id)
        if not job:
            self._recent_submissions.pop(dedupe_key, None)
            return None
        if now - job.created_at > self.duplicate_window:
            self._recent_submissions.pop(dedupe_key, None)
            return None
        return job

    def _prune_recent_submissions(self, now: float) -> None:
        for key, job_id in list(self._recent_submissions.items()):
            job = self._jobs.get(job_id)
            if not job or now - job.created_at > self.duplicate_window:
                self._recent_submissions.pop(key, None)

    @staticmethod
    def _args_signature(args: Dict[str, Any]) -> str:
        return json.dumps(args or {}, sort_keys=True, ensure_ascii=False, default=str)

    def get_job(self, job_id: str) -> Optional[ImageGenerationJob]:
        with self._lock:
            return self._jobs.get(job_id)

    def queue_position(self, job: ImageGenerationJob) -> int:
        with self._lock:
            q = self._queues.get(job.actor_id)
            if not q:
                return 0 if job.status == "running" else 1
            queued = list(q.queue)
            for index, queued_job in enumerate(queued, start=1):
                if queued_job.job_id == job.job_id:
                    return index
        return 0 if job.status == "running" else 1

    def shutdown(self, wait: bool = False) -> None:
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def _run_actor_queue(self, actor_id: str) -> None:
        try:
            while True:
                with self._lock:
                    q = self._queues.get(actor_id)
                    if q is None or q.empty():
                        self._active_workers.discard(actor_id)
                        return
                    job = q.get_nowait()
                self._run_job(job)
        except Exception as e:
            logger.error(f"[ImageGenerationJobManager] worker failed for actor={actor_id}: {e}", exc_info=True)
            with self._lock:
                self._active_workers.discard(actor_id)

    def _run_job(self, job: ImageGenerationJob) -> None:
        job.status = "running"
        job.started_at = time.time()
        try:
            result = self._invoke_generator(job)
            images = result.get("images") or []
            first = images[0] if images else {}
            image_path = first.get("url") if isinstance(first, dict) else None
            if not image_path:
                raise RuntimeError(result.get("error") or "generator completed without an image")
            job.output_path = os.path.abspath(expand_path(str(image_path)))
            job.status = "succeeded"
            self._send_completion(job)
        except subprocess.TimeoutExpired:
            job.status = "failed"
            job.error = f"生图任务超时（超过 {self.task_timeout} 秒）"
            self._send_failure(job)
        except Exception as e:
            job.status = "failed"
            job.error = str(e)
            self._send_failure(job)
        finally:
            job.completed_at = time.time()

    def _invoke_generator(self, job: ImageGenerationJob) -> Dict[str, Any]:
        if not os.path.exists(self.script_path):
            raise RuntimeError(f"image generation script not found: {self.script_path}")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        self._ensure_codex_auth_env(env)
        self._ensure_default_broker_env(env, job.args.get("runtime"))
        completed = subprocess.run(
            [sys.executable, self.script_path, json.dumps(job.args, ensure_ascii=False)],
            cwd=os.path.dirname(self.script_path),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=self.task_timeout,
            env=env,
        )
        if completed.stderr:
            logger.info(f"[ImageGenerationJobManager] generator stderr for {job.job_id}: {completed.stderr[-2000:]}")

        stdout = completed.stdout.strip()
        try:
            payload = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"generator returned invalid JSON: {e}; stdout={stdout[-500:]}")

        if completed.returncode != 0 or payload.get("error"):
            raise RuntimeError(payload.get("error") or f"generator exited with code {completed.returncode}")
        return payload

    def _build_output_dir(self, profile: Any, memory_user_id: str, job_id: str) -> str:
        user_files = os.path.join(self.workspace_root, "users", memory_user_id, "files")
        return os.path.abspath(os.path.join(user_files, "image-generation", job_id))

    def _clean_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        allowed = ("prompt", "size", "aspect_ratio", "quality", "image_url", "runtime")
        cleaned = {k: v for k, v in (args or {}).items() if k in allowed and v not in (None, "")}
        cleaned["prompt"] = str(cleaned.get("prompt", "")).strip()
        runtime = self._configured_runtime()
        if runtime:
            cleaned.setdefault("runtime", runtime)
        return cleaned

    def _ensure_default_broker_env(self, env: Dict[str, str], runtime: Any = None) -> None:
        broker_env_keys = (
            "SKILL_IMAGE_GENERATION_BROKER_COMMAND_JSON",
            "IMAGE_GENERATION_BROKER_COMMAND_JSON",
            "SKILL_IMAGE_GENERATION_BROKER_COMMAND",
            "IMAGE_GENERATION_BROKER_COMMAND",
            "CODEX_IMAGE_GEN_COMMAND",
        )
        if any(env.get(key) for key in broker_env_keys):
            return
        if str(runtime or "").strip().lower() not in BROKER_RUNTIMES:
            return

        broker_path = os.path.join(os.path.dirname(self.script_path), "codex_cli_broker.py")
        if not os.path.exists(broker_path):
            return
        env["SKILL_IMAGE_GENERATION_BROKER_COMMAND_JSON"] = json.dumps(
            [sys.executable, broker_path],
            ensure_ascii=False,
        )

    @staticmethod
    def _ensure_codex_auth_env(env: Dict[str, str]) -> None:
        if env.get("CODEX_AUTH_FILE"):
            return
        skill_conf = conf().get("skill", {})
        if not isinstance(skill_conf, dict):
            return
        image_conf = skill_conf.get("image-generation") or skill_conf.get("image_generation")
        if not isinstance(image_conf, dict):
            return
        auth_file = (
            image_conf.get("codex_auth_file")
            or image_conf.get("auth_file")
            or image_conf.get("codex_auth_path")
            or ""
        )
        auth_file = str(auth_file).strip()
        if auth_file:
            env["CODEX_AUTH_FILE"] = os.path.abspath(os.path.expanduser(auth_file))

    @staticmethod
    def _configured_runtime() -> str:
        skill_conf = conf().get("skill", {})
        if isinstance(skill_conf, dict):
            image_conf = skill_conf.get("image-generation") or skill_conf.get("image_generation")
            if isinstance(image_conf, dict):
                runtime = str(image_conf.get("runtime") or "").strip()
                if runtime:
                    return runtime
        runtime = str(conf().get("image_generation_runtime") or "").strip()
        return runtime or DEFAULT_IMAGE_GENERATION_RUNTIME

    def _snapshot_context(self, context: Any, profile: Any) -> Dict[str, Any]:
        msg = self._context_get(context, "msg")
        context_token = getattr(msg, "context_token", None) if msg is not None else None
        snapshot = {
            "channel_type": self._context_get(context, "channel_type", getattr(profile, "channel_type", "unknown")),
            "receiver": self._context_get(context, "receiver"),
            "isgroup": bool(self._context_get(context, "isgroup", False)),
            "session_id": self._context_get(context, "session_id", getattr(profile, "conversation_id", "")),
            "actor_id": getattr(profile, "actor_id", self._context_get(context, "actor_id", "")),
            "memory_user_id": getattr(profile, "memory_user_id", self._context_get(context, "memory_user_id", "")),
        }
        if context_token:
            snapshot["context_token"] = context_token
        return snapshot

    @staticmethod
    def _context_get(context: Any, key: str, default: Any = None) -> Any:
        if context is None:
            return default
        try:
            return context.get(key, default)
        except Exception:
            return default

    def _build_send_context(self, job: ImageGenerationJob, content: str = "") -> Context:
        snapshot = job.context_snapshot
        context = Context(ContextType.TEXT, content)
        context["receiver"] = snapshot.get("receiver")
        context["isgroup"] = bool(snapshot.get("isgroup", False))
        context["session_id"] = snapshot.get("session_id") or snapshot.get("receiver")
        context["channel_type"] = snapshot.get("channel_type", "unknown")
        if snapshot.get("context_token"):
            context["msg"] = SimpleNamespace(context_token=snapshot["context_token"])
        else:
            context["msg"] = None
        return context

    def _get_channel(self, channel_type: str):
        try:
            from app import get_channel_manager

            manager = get_channel_manager()
            channel = manager.get_channel(channel_type) if manager else None
            if channel is not None:
                return channel
        except Exception as e:
            logger.warning(f"[ImageGenerationJobManager] failed to get running channel '{channel_type}': {e}")

        from channel.channel_factory import create_channel

        return create_channel(channel_type)

    def _send_completion(self, job: ImageGenerationJob) -> None:
        text = f"生图完成（任务 {job.job_id}），图片已生成。"
        self._send_reply(job, Reply(ReplyType.TEXT, text), text)
        self._send_reply(job, Reply(ReplyType.IMAGE_URL, f"file://{job.output_path}"), "")
        self._remember_output(job, "生图完成，图片已发送。")

    def _send_failure(self, job: ImageGenerationJob) -> None:
        error = job.error or "未知错误"
        text = f"生图失败（任务 {job.job_id}）：{error}\n我不会用相同参数反复重试。"
        self._send_reply(job, Reply(ReplyType.TEXT, text), text)
        self._remember_output(job, text)

    def _send_reply(self, job: ImageGenerationJob, reply: Reply, content: str) -> None:
        channel_type = str(job.context_snapshot.get("channel_type") or "unknown")
        receiver = job.context_snapshot.get("receiver")
        if not receiver:
            logger.error(f"[ImageGenerationJobManager] missing receiver for job={job.job_id}")
            return
        try:
            channel = self._get_channel(channel_type)
            channel.send(reply, self._build_send_context(job, content))
        except Exception as e:
            logger.error(f"[ImageGenerationJobManager] failed to send job={job.job_id}: {e}", exc_info=True)

    def _remember_output(self, job: ImageGenerationJob, content: str) -> None:
        if not self.agent_bridge or not content:
            return
        remember = getattr(self.agent_bridge, "remember_scheduled_output", None)
        if not remember:
            return
        try:
            remember(
                str(job.context_snapshot.get("session_id") or ""),
                content,
                channel_type=str(job.context_snapshot.get("channel_type") or ""),
                task_description=f"image_generation_task {job.job_id}",
            )
        except Exception as e:
            logger.warning(f"[ImageGenerationJobManager] failed to remember output for {job.job_id}: {e}")


_manager: Optional[ImageGenerationJobManager] = None
_manager_lock = threading.Lock()


def get_image_generation_job_manager(agent_bridge=None) -> ImageGenerationJobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = ImageGenerationJobManager(agent_bridge)
        elif agent_bridge is not None:
            _manager.agent_bridge = agent_bridge
        return _manager
