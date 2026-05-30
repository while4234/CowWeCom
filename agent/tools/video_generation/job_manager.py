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
JOB_STATE_FILE = "job_state.json"
RECOVERABLE_STATUSES = {"queued", "running"}
RESTART_RECOVERY_ERROR = (
    "Grok video generation was interrupted because CowAgent restarted. "
    "Please send the video request again."
)


@dataclass
class GrokVideoGenerationJob:
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


class GrokVideoGenerationJobManager:
    """Runs Grok video generation in background queues with bounded parallelism."""

    def __init__(
        self,
        agent_bridge=None,
        *,
        script_path: Optional[str] = None,
        workspace_root: Optional[str] = None,
        global_workers: Optional[int] = None,
        actor_workers: Optional[int] = None,
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
                    "grok-video-generation",
                    "scripts",
                    "generate.py",
                )
            )
        )
        self.global_workers = max(
            int(global_workers or conf().get("grok_video_generation_global_workers", 2) or 2),
            1,
        )
        self.actor_workers = max(
            int(
                actor_workers
                if actor_workers is not None
                else conf().get("grok_video_generation_actor_workers", self.global_workers)
                or self.global_workers
            ),
            1,
        )
        self.task_timeout = max(
            int(task_timeout or conf().get("grok_video_generation_task_timeout", 900) or 900),
            1,
        )
        self.duplicate_window = max(
            int(
                duplicate_window
                if duplicate_window is not None
                else conf().get("grok_video_generation_duplicate_window", 120) or 120
            ),
            0,
        )
        self._executor = ThreadPoolExecutor(
            max_workers=self.global_workers,
            thread_name_prefix="grokvideo",
        )
        self._lock = threading.RLock()
        self._queues: Dict[str, "queue.Queue[GrokVideoGenerationJob]"] = {}
        self._active_workers: Dict[str, int] = {}
        self._jobs: Dict[str, GrokVideoGenerationJob] = {}
        self._recent_submissions: Dict[tuple[str, str], str] = {}

    def submit(self, args: Dict[str, Any], context: Any, profile: Any) -> GrokVideoGenerationJob:
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
                        "[GrokVideoGenerationJobManager] duplicate video task ignored: actor=%s existing_job=%s",
                        actor_id,
                        existing.job_id,
                    )
                    return existing

            job_id = uuid.uuid4().hex[:12]
            output_dir = self._build_output_dir(memory_user_id, job_id)
            os.makedirs(output_dir, exist_ok=True)

            job_args = dict(job_args)
            job_args["output_dir"] = output_dir
            job = GrokVideoGenerationJob(
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
            self._persist_job_state(job)
            if self.duplicate_window:
                self._recent_submissions[dedupe_key] = job_id
            self._start_actor_workers_locked(actor_id)
        return job

    def queue_position(self, job: GrokVideoGenerationJob) -> int:
        with self._lock:
            q = self._queues.get(job.actor_id)
            if not q:
                return 0 if job.status == "running" else 1
            for index, queued_job in enumerate(list(q.queue), start=1):
                if queued_job.job_id == job.job_id:
                    return index
        return 0 if job.status == "running" else 1

    def shutdown(self, wait: bool = False) -> None:
        if not wait:
            with self._lock:
                wait = all(job.status not in {"queued", "running"} for job in self._jobs.values())
        self._executor.shutdown(wait=wait, cancel_futures=True)

    def recover_unfinished_jobs(self, *, notify: bool = True) -> list[GrokVideoGenerationJob]:
        recovered: list[GrokVideoGenerationJob] = []
        for state_path in self._iter_state_files():
            state = self._load_state_file(state_path)
            if not state or state.get("status") not in RECOVERABLE_STATUSES:
                continue
            job = self._job_from_state(state, state_path)
            if job is None:
                continue

            output_path = self._recoverable_output_path(job)
            if output_path:
                job.output_path = output_path
                self._record_hidden_prompt(job)
                delivered = self._send_completion(job) if notify else True
                job.status = "succeeded" if delivered else "delivery_failed"
                if not delivered:
                    job.error = "video generation completed, but delivery failed"
            else:
                job.status = "failed"
                job.error = RESTART_RECOVERY_ERROR
                delivered = self._send_failure(job) if notify else True
                if not delivered:
                    job.status = "delivery_failed"
            job.completed_at = time.time()
            self._persist_job_state(job)
            recovered.append(job)
        if recovered:
            logger.warning(
                "[GrokVideoGenerationJobManager] recovered %s unfinished video job(s)",
                len(recovered),
            )
        return recovered

    def _run_actor_queue(self, actor_id: str) -> None:
        try:
            while True:
                with self._lock:
                    q = self._queues.get(actor_id)
                    if q is None or q.empty():
                        self._finish_actor_worker_locked(actor_id)
                        return
                    job = q.get_nowait()
                self._run_job(job)
        except Exception as e:
            logger.error("[GrokVideoGenerationJobManager] worker failed for actor=%s: %s", actor_id, e, exc_info=True)
            with self._lock:
                self._finish_actor_worker_locked(actor_id)
                self._start_actor_workers_locked(actor_id)

    def _start_actor_workers_locked(self, actor_id: str) -> None:
        q = self._queues.get(actor_id)
        if q is None:
            return
        active = self._active_workers.get(actor_id, 0)
        target = min(self.actor_workers, active + q.qsize())
        while active < target:
            active += 1
            self._active_workers[actor_id] = active
            self._executor.submit(self._run_actor_queue, actor_id)

    def _finish_actor_worker_locked(self, actor_id: str) -> None:
        active = self._active_workers.get(actor_id, 0) - 1
        if active > 0:
            self._active_workers[actor_id] = active
        else:
            self._active_workers.pop(actor_id, None)

    def _run_job(self, job: GrokVideoGenerationJob) -> None:
        job.status = "running"
        job.started_at = time.time()
        self._persist_job_state(job)
        final_status = "failed"
        final_error: Optional[str] = None
        try:
            result = self._invoke_generator(job)
            videos = result.get("videos") or result.get("video") or []
            if isinstance(videos, dict):
                videos = [videos]
            first = videos[0] if videos else {}
            video_path = first.get("url") if isinstance(first, dict) else None
            if not video_path:
                raise RuntimeError(result.get("error") or "generator completed without a video")
            job.output_path = os.path.abspath(expand_path(str(video_path)))
            self._persist_job_state(job)
            self._record_hidden_prompt(job)
            delivered = self._send_completion(job)
            final_status = "succeeded" if delivered else "delivery_failed"
            if not delivered:
                final_error = "video generation completed, but delivery failed"
        except subprocess.TimeoutExpired:
            final_error = f"Grok video generation timed out after {self.task_timeout} seconds."
            job.error = final_error
            self._record_hidden_prompt(job, status="failed", error=final_error)
            self._persist_job_state(job)
            if not self._send_failure(job):
                final_status = "delivery_failed"
        except Exception as e:
            final_error = str(e)
            job.error = final_error
            self._record_hidden_prompt(job, status="failed", error=final_error)
            self._persist_job_state(job)
            if not self._send_failure(job):
                final_status = "delivery_failed"

        job.error = final_error
        job.completed_at = time.time()
        self._persist_job_state(job, status=final_status)
        job.status = final_status

    def _invoke_generator(self, job: GrokVideoGenerationJob) -> Dict[str, Any]:
        if not os.path.exists(self.script_path):
            raise RuntimeError(f"Grok video generation script not found: {self.script_path}")

        env = os.environ.copy()
        env["PYTHONIOENCODING"] = "utf-8"
        env.setdefault("COWWECHAT_ROOT", str(Path(__file__).resolve().parents[3]))
        try:
            from integrations.hermes_xai.proxy import apply_xai_proxy_env

            apply_xai_proxy_env(env)
        except Exception as exc:
            logger.debug("[GrokVideoGenerationJobManager] xAI proxy env sync skipped: %s", exc)
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
            logger.info("[GrokVideoGenerationJobManager] generator stderr for %s: %s", job.job_id, completed.stderr[-2000:])

        stdout = completed.stdout.strip()
        try:
            payload = json.loads(stdout) if stdout else {}
        except json.JSONDecodeError as e:
            raise RuntimeError(f"generator returned invalid JSON: {e}; stdout={stdout[-500:]}")

        if completed.returncode != 0 or payload.get("error"):
            raise RuntimeError(payload.get("error") or f"generator exited with code {completed.returncode}")
        return payload

    def _get_recent_duplicate(self, dedupe_key: tuple[str, str], now: float) -> Optional[GrokVideoGenerationJob]:
        job_id = self._recent_submissions.get(dedupe_key)
        if not job_id:
            return None
        job = self._jobs.get(job_id)
        if not job:
            self._recent_submissions.pop(dedupe_key, None)
            return None
        if now - job.created_at > self.duplicate_window or job.status not in {"queued", "running"}:
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

    def _build_output_dir(self, memory_user_id: str, job_id: str) -> str:
        user_files = os.path.join(self.workspace_root, "users", memory_user_id, "files")
        return os.path.abspath(os.path.join(user_files, "grok-video-generation", job_id))

    def _state_file_path(self, job: GrokVideoGenerationJob) -> str:
        return os.path.join(job.output_dir, JOB_STATE_FILE)

    def _persist_job_state(self, job: GrokVideoGenerationJob, *, status: Optional[JobStatus] = None) -> None:
        try:
            os.makedirs(job.output_dir, exist_ok=True)
            state_path = self._state_file_path(job)
            tmp_path = f"{state_path}.{uuid.uuid4().hex}.tmp"
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(self._job_state_payload(job, status=status), f, ensure_ascii=False, indent=2, sort_keys=True)
            os.replace(tmp_path, state_path)
        except Exception as e:
            if "tmp_path" in locals() and os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass
            logger.warning("[GrokVideoGenerationJobManager] failed to persist job state %s: %s", job.job_id, e)

    @staticmethod
    def _job_state_payload(job: GrokVideoGenerationJob, *, status: Optional[JobStatus] = None) -> Dict[str, Any]:
        return {
            "job_id": job.job_id,
            "actor_id": job.actor_id,
            "memory_user_id": job.memory_user_id,
            "output_dir": job.output_dir,
            "context_snapshot": job.context_snapshot,
            "status": status if status is not None else job.status,
            "created_at": job.created_at,
            "started_at": job.started_at,
            "completed_at": job.completed_at,
            "output_path": job.output_path,
            "error": job.error,
        }

    def _iter_state_files(self):
        root = Path(self.workspace_root) / "users"
        if not root.exists():
            return
        yield from (str(path) for path in root.glob(f"*/files/grok-video-generation/*/{JOB_STATE_FILE}") if path.is_file())

    @staticmethod
    def _load_state_file(state_path: str) -> Optional[Dict[str, Any]]:
        try:
            with open(state_path, "r", encoding="utf-8") as f:
                state = json.load(f)
            return state if isinstance(state, dict) else None
        except Exception as e:
            logger.warning("[GrokVideoGenerationJobManager] failed to read job state %s: %s", state_path, e)
            return None

    def _job_from_state(self, state: Dict[str, Any], state_path: str) -> Optional[GrokVideoGenerationJob]:
        job_id = str(state.get("job_id") or "").strip()
        actor_id = str(state.get("actor_id") or "").strip()
        memory_user_id = str(state.get("memory_user_id") or "").strip()
        output_dir = str(state.get("output_dir") or os.path.dirname(state_path)).strip()
        context_snapshot = state.get("context_snapshot") or {}
        if not job_id or not actor_id or not memory_user_id or not isinstance(context_snapshot, dict):
            logger.warning("[GrokVideoGenerationJobManager] invalid job state skipped: %s", state_path)
            return None
        return GrokVideoGenerationJob(
            job_id=job_id,
            actor_id=actor_id,
            memory_user_id=memory_user_id,
            args={},
            output_dir=os.path.abspath(expand_path(output_dir)),
            context_snapshot=context_snapshot,
            status=str(state.get("status") or "queued"),
            created_at=self._optional_float(state.get("created_at")) or time.time(),
            started_at=self._optional_float(state.get("started_at")),
            completed_at=self._optional_float(state.get("completed_at")),
            output_path=state.get("output_path"),
            error=state.get("error"),
        )

    def _recoverable_output_path(self, job: GrokVideoGenerationJob) -> Optional[str]:
        candidates = []
        if job.output_path:
            candidates.append(str(job.output_path))
        try:
            output_dir = Path(job.output_dir)
            for pattern in ("*.mp4", "*.mov", "*.webm", "*.m4v"):
                candidates.extend(str(path) for path in output_dir.glob(pattern))
        except Exception:
            pass
        for candidate in candidates:
            path = os.path.abspath(expand_path(candidate))
            if os.path.isfile(path):
                return path
        return None

    @staticmethod
    def _optional_float(value: Any) -> Optional[float]:
        if value is None:
            return None
        try:
            return float(value)
        except (TypeError, ValueError):
            return None

    def _clean_args(self, args: Dict[str, Any]) -> Dict[str, Any]:
        allowed = ("prompt", "image_url", "aspect_ratio", "duration", "resolution", "quality", "prompt_enhancement")
        cleaned = {k: v for k, v in (args or {}).items() if k in allowed and v not in (None, "", [])}
        cleaned["prompt"] = str(cleaned.get("prompt", "")).strip()
        return cleaned

    def _snapshot_context(self, context: Any, profile: Any) -> Dict[str, Any]:
        msg = self._context_get(context, "msg")
        context_token = getattr(msg, "context_token", None) if msg is not None else None
        snapshot = {
            "channel_type": self._context_get(context, "channel_type", getattr(profile, "channel_type", "unknown")),
            "receiver": self._context_get(context, "receiver"),
            "request_id": self._context_get(context, "request_id"),
            "isgroup": bool(self._context_get(context, "isgroup", False)),
            "session_id": self._context_get(context, "session_id", getattr(profile, "conversation_id", "")),
            "actor_id": getattr(profile, "actor_id", self._context_get(context, "actor_id", "")),
            "memory_user_id": getattr(profile, "memory_user_id", self._context_get(context, "memory_user_id", "")),
        }
        if context_token:
            snapshot["context_token"] = context_token
        for key in ("_discord_channel_id", "_discord_guild_id"):
            value = self._context_get(context, key)
            if value:
                snapshot[key] = value
        return snapshot

    @staticmethod
    def _context_get(context: Any, key: str, default: Any = None) -> Any:
        if context is None:
            return default
        try:
            return context.get(key, default)
        except Exception:
            return default

    def _build_send_context(self, job: GrokVideoGenerationJob, content: str = "") -> Context:
        snapshot = job.context_snapshot
        context = Context(ContextType.TEXT, content)
        context["receiver"] = snapshot.get("receiver")
        context["request_id"] = snapshot.get("request_id")
        context["isgroup"] = bool(snapshot.get("isgroup", False))
        context["session_id"] = snapshot.get("session_id") or snapshot.get("receiver")
        context["channel_type"] = snapshot.get("channel_type", "unknown")
        if snapshot.get("_discord_channel_id"):
            context["_discord_channel_id"] = snapshot.get("_discord_channel_id")
        if snapshot.get("_discord_guild_id"):
            context["_discord_guild_id"] = snapshot.get("_discord_guild_id")
        context["msg"] = SimpleNamespace(context_token=snapshot["context_token"]) if snapshot.get("context_token") else None
        return context

    def _get_channel(self, channel_type: str):
        try:
            from app import get_channel_manager

            manager = get_channel_manager()
            channel = manager.get_channel(channel_type) if manager else None
            if channel is not None:
                return channel
        except Exception as e:
            logger.warning("[GrokVideoGenerationJobManager] failed to get running channel '%s': %s", channel_type, e)

        from channel.channel_factory import create_channel

        return create_channel(channel_type)

    def _send_completion(self, job: GrokVideoGenerationJob) -> bool:
        text = f"Grok video generation finished (task {job.job_id})."
        text_delivered = self._send_reply(job, Reply(ReplyType.TEXT, text), text)
        video_delivered = self._send_reply(job, Reply(ReplyType.VIDEO, job.output_path), "")
        delivered = text_delivered and video_delivered
        if delivered:
            self._remember_output(job, "Grok video generation finished and the video was sent.")
        return delivered

    def _send_failure(self, job: GrokVideoGenerationJob) -> bool:
        error = job.error or "unknown error"
        text = f"Grok video generation failed (task {job.job_id}): {error}"
        delivered = self._send_reply(job, Reply(ReplyType.TEXT, text), text)
        if delivered:
            self._remember_output(job, text)
        return delivered

    def _send_reply(self, job: GrokVideoGenerationJob, reply: Reply, content: str) -> bool:
        channel_type = str(job.context_snapshot.get("channel_type") or "unknown")
        receiver = job.context_snapshot.get("receiver")
        if not receiver:
            logger.error("[GrokVideoGenerationJobManager] missing receiver for job=%s", job.job_id)
            return False
        try:
            channel = self._get_channel(channel_type)
            result = channel.send(reply, self._build_send_context(job, content))
            if result is False or (isinstance(result, dict) and result.get("ok") is False):
                logger.error(
                    "[GrokVideoGenerationJobManager] channel reported delivery failure for job=%s channel=%s receiver=%s result=%s",
                    job.job_id,
                    channel_type,
                    receiver,
                    result,
                )
                return False
            return True
        except Exception as e:
            logger.error("[GrokVideoGenerationJobManager] failed to send job=%s: %s", job.job_id, e, exc_info=True)
            return False

    def _remember_output(self, job: GrokVideoGenerationJob, content: str) -> None:
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
                task_description=f"grok_video_generation_task {job.job_id}",
            )
        except Exception as e:
            logger.warning("[GrokVideoGenerationJobManager] failed to remember output for %s: %s", job.job_id, e)

    def _record_hidden_prompt(
        self,
        job: GrokVideoGenerationJob,
        *,
        status: str = "",
        error: str = "",
    ) -> None:
        try:
            from common.image_prompt_enhancer import read_prompt_metadata, record_prompt_history

            metadata = read_prompt_metadata(job.output_dir)
            if not metadata:
                metadata = self._fallback_prompt_metadata(job)
            metadata = dict(metadata)
            if status:
                metadata["generation_status"] = status
            if error:
                metadata["generation_error"] = str(error)
            record_prompt_history(
                workspace_root=self.workspace_root,
                memory_user_id=job.memory_user_id,
                session_id=str(job.context_snapshot.get("session_id") or ""),
                job_id=job.job_id,
                output_path=str(job.output_path or ""),
                metadata=metadata,
            )
        except Exception as e:
            logger.warning("[GrokVideoGenerationJobManager] failed to record hidden prompt for %s: %s", job.job_id, e)

    @staticmethod
    def _fallback_prompt_metadata(job: GrokVideoGenerationJob) -> Dict[str, Any]:
        prompt = str((job.args or {}).get("prompt") or "").strip()
        return {
            "version": "grok-video-fallback-v1",
            "enhanced": False,
            "disabled_reason": "prompt_metadata_missing",
            "target": "grok",
            "media_type": "video",
            "runtime": "grok_video",
            "original_prompt": prompt,
            "enhanced_prompt": prompt,
            "created_at": time.time(),
        }


_manager: Optional[GrokVideoGenerationJobManager] = None
_manager_lock = threading.Lock()


def get_grok_video_generation_job_manager(agent_bridge=None) -> GrokVideoGenerationJobManager:
    global _manager
    with _manager_lock:
        if _manager is None:
            _manager = GrokVideoGenerationJobManager(agent_bridge)
        elif agent_bridge is not None:
            _manager.agent_bridge = agent_bridge
        return _manager
