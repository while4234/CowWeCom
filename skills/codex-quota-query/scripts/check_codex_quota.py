#!/usr/bin/env python3
"""Query Codex quota through the official Codex app-server protocol."""

from __future__ import annotations

import argparse
import base64
import json
import os
import queue
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
if hasattr(sys.stderr, "reconfigure"):
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_TIMEOUT_MS = 45000
SOURCE_NAME = "codex-app-server"


class AppServerError(RuntimeError):
    pass


class AppServerClient:
    def __init__(self, command: List[str], *, cwd: Path, env: Mapping[str, str]) -> None:
        self._proc = subprocess.Popen(
            command,
            cwd=str(cwd),
            env=dict(env),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        self._next_id = 1
        self._stdout: "queue.Queue[str]" = queue.Queue()
        self._stderr: "queue.Queue[str]" = queue.Queue()
        self._stdout_thread = threading.Thread(target=self._read_lines, args=(self._proc.stdout, self._stdout), daemon=True)
        self._stderr_thread = threading.Thread(target=self._read_lines, args=(self._proc.stderr, self._stderr), daemon=True)
        self._stdout_thread.start()
        self._stderr_thread.start()

    def request(self, method: str, params: Optional[Mapping[str, Any]], *, timeout_ms: int) -> Any:
        request_id = self._next_id
        self._next_id += 1
        message: Dict[str, Any] = {"id": request_id, "method": method}
        if params is not None:
            message["params"] = dict(params)
        self._write_message(message)

        deadline = time.monotonic() + max(0.001, timeout_ms / 1000.0)
        while time.monotonic() < deadline:
            line = self._read_stdout_line(deadline)
            if line is None:
                break
            parsed = _parse_json_line(line)
            if not parsed:
                continue
            if self._handle_server_request(parsed):
                continue
            if parsed.get("id") != request_id:
                continue
            if "error" in parsed:
                error = as_record(parsed.get("error"))
                message_text = str(error.get("message") or error or "unknown app-server error")
                raise AppServerError(message_text)
            return parsed.get("result")

        raise TimeoutError(f"Codex app-server request timed out: {method}")

    def close(self) -> None:
        try:
            if self._proc.stdin:
                self._proc.stdin.close()
        except Exception:
            pass
        if self._proc.poll() is None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except Exception:
                self._proc.kill()

    def stderr_tail(self, limit: int = 400) -> str:
        parts: List[str] = []
        while True:
            try:
                parts.append(self._stderr.get_nowait())
            except queue.Empty:
                break
        text = "".join(parts).strip()
        return text[-limit:]

    def _write_message(self, message: Mapping[str, Any]) -> None:
        if self._proc.poll() is not None:
            raise AppServerError(f"Codex app-server exited with code {self._proc.returncode}")
        if not self._proc.stdin:
            raise AppServerError("Codex app-server stdin is not available")
        self._proc.stdin.write(json.dumps(message, ensure_ascii=False, separators=(",", ":")) + "\n")
        self._proc.stdin.flush()

    def _read_stdout_line(self, deadline: float) -> Optional[str]:
        remaining = max(0.001, deadline - time.monotonic())
        try:
            return self._stdout.get(timeout=remaining)
        except queue.Empty:
            return None

    def _handle_server_request(self, message: Mapping[str, Any]) -> bool:
        if "method" not in message or "id" not in message:
            return False
        if "result" in message or "error" in message:
            return False
        self._write_message({"id": message["id"], "result": _server_request_default_response(str(message.get("method") or ""))})
        return True

    @staticmethod
    def _read_lines(pipe: Any, target: "queue.Queue[str]") -> None:
        if pipe is None:
            return
        for line in iter(pipe.readline, ""):
            target.put(line)


def default_skill_dir() -> Path:
    return Path(__file__).resolve().parent.parent


def project_root_from(project_dir: Path) -> Path:
    if (project_dir / "skills" / "codex-quota-query").exists():
        return project_dir
    if project_dir.name == "codex-quota-query" and project_dir.parent.name == "skills":
        return project_dir.parent.parent
    return project_dir


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Query GPT/Codex quota without opening a browser.")
    parser.add_argument("--project-dir", default=str(default_skill_dir()), help="Skill directory or CowAgent project root")
    parser.add_argument("--codex-bin", default="", help="Override the Codex executable")
    parser.add_argument("--codex-command-json", default="", help=argparse.SUPPRESS)
    parser.add_argument("--format", choices=["text", "json"], default="text")
    parser.add_argument("--timeout-ms", type=int, default=None)
    parser.add_argument("--show-account", action="store_true", help="Show the full account label instead of masking it")
    parser.add_argument("--mock-account-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--mock-rate-limits-file", default="", help=argparse.SUPPRESS)
    parser.add_argument("--now-ms", default="", help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    project_dir = Path(args.project_dir).expanduser().resolve()
    project_root = project_root_from(project_dir)
    timeout_ms = read_timeout_ms(args.timeout_ms)

    if args.mock_account_file or args.mock_rate_limits_file:
        account = read_json_file(Path(args.mock_account_file), {}) if args.mock_account_file else {}
        limits = read_json_file(Path(args.mock_rate_limits_file), {}) if args.mock_rate_limits_file else {}
        payload = combined_payload(account, limits, show_account=args.show_account)
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.format == "json" else format_quota(payload))
        return 0

    try:
        payload = query_codex_quota(
            project_root,
            args.codex_bin,
            args.codex_command_json,
            timeout_ms,
            show_account=args.show_account,
        )
    except subprocess.TimeoutExpired:
        emit_error(f"GPT/Codex quota query timed out after {timeout_ms // 1000} seconds.", args.format)
        return 124
    except TimeoutError as exc:
        emit_error(str(exc), args.format)
        return 124
    except Exception as exc:
        emit_error(str(exc), args.format)
        return 1

    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.format == "json" else format_quota(payload))
    return 0


def query_codex_quota(
    project_root: Path,
    codex_bin: str,
    codex_command_json: str,
    timeout_ms: int,
    *,
    show_account: bool = False,
) -> Dict[str, Any]:
    env = prepare_env(project_root)
    command = resolve_codex_command(codex_bin, codex_command_json) + ["app-server", "--listen", "stdio://"]
    auth_tokens = read_chatgpt_auth_tokens(env, project_root)
    client = AppServerClient(command, cwd=project_root, env=env)
    try:
        per_request_timeout = max(1000, timeout_ms)
        client.request(
            "initialize",
            {
                "clientInfo": {
                    "name": "cowagent",
                    "title": "CowAgent",
                    "version": "codex-quota-query",
                },
                "capabilities": {
                    "experimentalApi": True,
                    "optOutNotificationMethods": [],
                },
            },
            timeout_ms=per_request_timeout,
        )
        client.request("account/login/start", auth_tokens, timeout_ms=per_request_timeout)
        account = client.request("account/read", {"refreshToken": False}, timeout_ms=per_request_timeout)
        limits = client.request("account/rateLimits/read", {}, timeout_ms=per_request_timeout)
        return combined_payload(account, limits, show_account=show_account)
    except Exception as exc:
        tail = client.stderr_tail()
        if tail:
            raise RuntimeError(f"{exc}; app-server stderr: {tail}") from exc
        raise
    finally:
        client.close()


def prepare_env(project_root: Path) -> Dict[str, str]:
    env = dict(os.environ)
    env.setdefault("PYTHONUTF8", "1")
    config = load_project_config(project_root)
    auth_file = configured_auth_file(config)
    if auth_file and not env.get("CODEX_AUTH_FILE"):
        env["CODEX_AUTH_FILE"] = auth_file
    return env


def load_project_config(project_root: Path) -> Dict[str, Any]:
    for name in ("config.json", "config-template.json"):
        path = project_root / name
        if not path.exists():
            continue
        try:
            value = json.loads(path.read_text(encoding="utf-8-sig"))
        except Exception:
            continue
        if isinstance(value, dict):
            return value
    return {}


def configured_auth_file(config: Mapping[str, Any]) -> str:
    for value in (
        text_value(config, "codex_auth_file"),
        text_value(config, "codex_auth_path"),
        text_value(as_record(as_record(as_record(config.get("llm_backend")).get("providers")).get("codex")), "auth_file"),
        text_value(as_record(as_record(config.get("skill")).get("image-generation")), "codex_auth_file"),
        text_value(as_record(as_record(config.get("skill")).get("image_generation")), "codex_auth_file"),
    ):
        if value:
            return value
    return ""


def resolve_auth_path(env: Mapping[str, str], project_root: Path) -> Path:
    auth_file = str(env.get("CODEX_AUTH_FILE") or "").strip()
    if auth_file:
        return Path(auth_file).expanduser()
    config_auth = configured_auth_file(load_project_config(project_root))
    if config_auth:
        return Path(config_auth).expanduser()
    codex_home = str(env.get("CODEX_HOME") or "").strip()
    if codex_home:
        return Path(codex_home).expanduser() / "auth.json"
    return Path.home() / ".codex" / "auth.json"


def read_chatgpt_auth_tokens(env: Mapping[str, str], project_root: Path) -> Dict[str, Any]:
    auth_path = resolve_auth_path(env, project_root)
    if not auth_path.exists():
        raise FileNotFoundError(
            "Codex auth file not found; set CODEX_AUTH_FILE or llm_backend.providers.codex.auth_file "
            "to the auth JSON created by Codex login."
        )
    payload = read_json_file(auth_path, {})
    tokens = as_record(payload.get("tokens"))
    access_token = str(tokens.get("access_token") or "").strip()
    if not access_token:
        raise RuntimeError("codex_auth_invalid: missing access token in Codex auth file")
    expires_at = token_expires_at(tokens)
    if expires_at and expires_at <= time.time():
        raise RuntimeError("codex_auth_expired: current Codex access token is expired; refresh Codex and retry")
    account_id = account_id_from_tokens(tokens)
    if not account_id:
        raise RuntimeError("codex_auth_invalid: missing ChatGPT account id in Codex auth tokens")
    return {
        "type": "chatgptAuthTokens",
        "accessToken": access_token,
        "chatgptAccountId": account_id,
        "chatgptPlanType": plan_type_from_auth(payload, tokens),
    }


def plan_type_from_auth(payload: Mapping[str, Any], tokens: Mapping[str, Any]) -> Optional[str]:
    for source in (payload, tokens):
        for key in ("plan_type", "planType", "chatgpt_plan_type", "chatgptPlanType"):
            value = text_value(source, key)
            if value:
                return value
    for token_key in ("id_token", "access_token"):
        claims = jwt_claims(str(tokens.get(token_key) or ""))
        for key in ("plan_type", "planType", "chatgpt_plan_type", "chatgptPlanType"):
            value = text_value(claims, key)
            if value:
                return value
        auth_claims = as_record(claims.get("https://api.openai.com/auth"))
        for key in ("plan_type", "planType", "chatgpt_plan_type", "chatgptPlanType"):
            value = text_value(auth_claims, key)
            if value:
                return value
    return None


def resolve_codex_binary(override: str) -> str:
    candidates: List[Path] = []
    if override:
        candidates.append(Path(override))
    env_binary = os.environ.get("CODEX_CLI_BINARY")
    if env_binary:
        candidates.append(Path(env_binary))
    which = shutil.which("codex")
    if which:
        candidates.append(Path(which))
    candidates.extend(candidate_codex_binaries())

    seen: set[str] = set()
    for candidate in candidates:
        try:
            path = candidate.expanduser().resolve()
        except OSError:
            continue
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if is_runnable_codex(path):
            return str(path)

    raise FileNotFoundError(
        "Codex CLI was not found. Install the official Codex app/CLI, ensure `codex` is on PATH, "
        "or set CODEX_CLI_BINARY."
    )


def resolve_codex_command(override: str, command_json: str) -> List[str]:
    if command_json:
        try:
            parsed = json.loads(command_json)
        except json.JSONDecodeError as exc:
            raise ValueError("CODEX app-server command JSON must be a JSON array") from exc
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("CODEX app-server command JSON must be a non-empty JSON array")
        return [str(part) for part in parsed]
    return [resolve_codex_binary(override)]


def candidate_codex_binaries() -> List[Path]:
    home = Path.home()
    candidates = [
        home / ".codex" / "bin" / "codex.cmd",
        home / ".codex" / ".sandbox-bin" / "codex.exe",
    ]
    vscode_ext = home / ".vscode" / "extensions"
    if vscode_ext.exists():
        candidates.extend(sorted(vscode_ext.glob("openai.chatgpt-*/bin/windows-x86_64/codex.exe"), reverse=True))
    local_app_data_raw = os.environ.get("LOCALAPPDATA")
    if local_app_data_raw:
        local_app_data = Path(local_app_data_raw)
        candidates.extend([
            local_app_data / "Programs" / "codex" / "codex.exe",
            local_app_data / "OpenAI" / "Codex" / "codex.exe",
        ])
    return candidates


def is_runnable_codex(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        result = subprocess.run(
            [str(path), "app-server", "--help"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8,
            check=False,
        )
    except Exception:
        return False
    return result.returncode == 0 and "app-server" in (result.stdout + result.stderr)


def read_timeout_ms(override: Optional[int]) -> int:
    if override is not None:
        return max(1, int(override))
    raw_timeout = os.environ.get("CODEX_QUOTA_TIMEOUT_MS")
    try:
        return max(1, int(raw_timeout)) if raw_timeout else DEFAULT_TIMEOUT_MS
    except ValueError:
        return DEFAULT_TIMEOUT_MS


def read_json_file(path: Path, fallback: Any) -> Any:
    if not str(path) or not path.exists():
        return fallback
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    return value


def emit_error(message: str, output_format: str) -> None:
    if output_format == "json":
        print(json.dumps({"ok": False, "error": message}, ensure_ascii=False, indent=2))
    else:
        print(message, file=sys.stderr)


def combined_payload(account_response: Any, rate_limit_response: Any, *, show_account: bool = False) -> Dict[str, Any]:
    rate_root = as_record(rate_limit_response)
    snapshots = collect_snapshots(rate_root)
    rate_limits = [normalize_snapshot(snapshot) for snapshot in snapshots]
    by_limit_id = as_record(rate_root.get("rateLimitsByLimitId") or rate_root.get("rate_limits_by_limit_id"))
    single = as_record(rate_root.get("rateLimits") or rate_root.get("rate_limits"))
    return {
        "ok": True,
        "source": SOURCE_NAME,
        "account": read_account(account_response, show_account=show_account),
        "summary": {
            "blocked": any(item.get("status") == "blocked" for item in rate_limits),
        },
        "rate_limits": rate_limits,
        "rateLimitsByLimitId": by_limit_id,
        "rate_limits_by_limit_id": by_limit_id,
        "rateLimits": single,
        "rate_limits_raw": single,
        "snapshots": snapshots,
    }


def read_account(account_response: Any, *, show_account: bool = False) -> Dict[str, Any]:
    outer = as_record(account_response)
    account = as_record(outer.get("account"))
    source = account if account else outer
    raw_email = str(source.get("email") or source.get("accountEmail") or "unknown")
    masked = raw_email if show_account else mask_email(raw_email)
    plan_type = str(source.get("planType") or source.get("plan_type") or "unknown")
    account_type = str(source.get("type") or "unknown")
    return {
        "email": masked,
        "label": masked,
        "email_masked": masked,
        "planType": plan_type,
        "plan_type": plan_type,
        "type": account_type,
        "requiresOpenaiAuth": outer.get("requiresOpenaiAuth") is True,
        "requires_openai_auth": outer.get("requiresOpenaiAuth") is True,
    }


def collect_snapshots(rate_limit_response: Mapping[str, Any]) -> List[Dict[str, Any]]:
    root = as_record(rate_limit_response)
    by_limit_id = as_record(root.get("rateLimitsByLimitId") or root.get("rate_limits_by_limit_id"))
    snapshots = [as_record(entry) for entry in by_limit_id.values() if as_record(entry)]
    if snapshots:
        return sorted(snapshots, key=snapshot_sort_key)
    single = as_record(root.get("rateLimits") or root.get("rate_limits"))
    return [single] if single else []


def snapshot_sort_key(snapshot: Mapping[str, Any]) -> tuple:
    limit_id = str(snapshot.get("limitId") or snapshot.get("limit_id") or "")
    return (0 if limit_id == "codex" else 1, label_for_snapshot(snapshot))


def normalize_snapshot(snapshot: Any) -> Dict[str, Any]:
    record = as_record(snapshot)
    limit_id = str(record.get("limitId") or record.get("limit_id") or "")
    reached_type = record.get("rateLimitReachedType") or record.get("rate_limit_reached_type") or ""
    windows = [
        window
        for window in (
            normalize_window("primary", record.get("primary")),
            normalize_window("secondary", record.get("secondary")),
        )
        if window is not None
    ]
    blocked = bool(reached_type) or any(number_or_none(item.get("used_percent")) and float(item["used_percent"]) >= 100 for item in windows)
    return {
        "limit_id": limit_id or None,
        "limit_name": "Codex" if limit_id == "codex" else label_for_snapshot(record),
        "status": "blocked" if blocked else "available",
        "reached_type": str(reached_type) if reached_type else None,
        "windows": windows,
    }


def normalize_window(name: str, value: Any) -> Optional[Dict[str, Any]]:
    record = as_record(value)
    if not record:
        return None
    used_percent = number_or_none(record.get("usedPercent") if "usedPercent" in record else record.get("used_percent"))
    window_minutes = number_or_none(
        record.get("windowDurationMins")
        if "windowDurationMins" in record
        else record.get("window_duration_mins", record.get("windowMinutes", record.get("window_minutes")))
    )
    reset_value = record.get("resetsAt") if "resetsAt" in record else record.get("resets_at")
    return {
        "name": name,
        "used_percent": used_percent,
        "remaining_percent": None if used_percent is None else max(0.0, round((100.0 - used_percent) * 10) / 10),
        "window_minutes": window_minutes,
        "reset_at": iso_reset_time(reset_value),
    }


def format_quota(payload: Mapping[str, Any]) -> str:
    account = as_record(payload.get("account"))
    rate_limits = payload.get("rate_limits") if isinstance(payload.get("rate_limits"), list) else []
    lines = [
        "GPT/Codex quota",
        f"- account: {account.get('label') or account.get('email_masked') or 'unknown'}",
        f"- plan: {account.get('plan_type') or 'unknown'}",
        f"- account type: {account.get('type') or 'unknown'}",
        f"- status: {'blocked' if as_record(payload.get('summary')).get('blocked') else 'available'}",
        "",
    ]
    if not rate_limits:
        lines.append("- no Codex rate limit data returned")
        return "\n".join(lines).strip()

    for limit in rate_limits:
        record = as_record(limit)
        reached = record.get("reached_type")
        lines.append(f"{record.get('limit_name') or 'Codex'}: {record.get('status') or 'unknown'}{f' ({reached})' if reached else ''}")
        windows = record.get("windows") if isinstance(record.get("windows"), list) else []
        for window in windows:
            period = as_record(window)
            lines.append(
                f"- {window_text(period.get('window_minutes'))}: "
                f"used {percent_text(period.get('used_percent'))}, "
                f"remaining {remaining_text(period.get('used_percent'))}, "
                f"resets {reset_text(period.get('reset_at'))}"
            )
        lines.append("")
    return "\n".join(lines).strip()


def label_for_snapshot(snapshot: Mapping[str, Any]) -> str:
    return str(snapshot.get("limitName") or snapshot.get("limit_name") or snapshot.get("limitId") or snapshot.get("limit_id") or "Codex")


def percent_text(value: Any) -> str:
    number = number_or_none(value)
    if number is None:
        return "unknown"
    return f"{number:g}%"


def remaining_text(value: Any) -> str:
    number = number_or_none(value)
    if number is None:
        return "unknown"
    return f"about {max(0.0, round((100.0 - number) * 10) / 10):g}%"


def window_text(value: Any) -> str:
    minutes = number_or_none(value)
    if minutes is None:
        return "window"
    minutes_int = int(minutes)
    if minutes_int % 10080 == 0:
        return f"{(minutes_int // 10080) * 7} day window"
    if minutes_int % 1440 == 0:
        return f"{minutes_int // 1440} day window"
    if minutes_int % 60 == 0:
        return f"{minutes_int // 60} hour window"
    return f"{minutes_int} minute window"


def reset_text(value: Any) -> str:
    iso_text = iso_reset_time(value)
    if not iso_text:
        return "unknown"
    try:
        dt = datetime.fromisoformat(iso_text.replace("Z", "+00:00")).astimezone(timezone.utc)
    except ValueError:
        return iso_text
    local = dt.astimezone()
    return f"{local.strftime('%Y-%m-%d %H:%M:%S')} {local.tzname() or ''}".strip()


def iso_reset_time(value: Any) -> Optional[str]:
    number = number_or_none(value)
    if number is not None and number > 0:
        seconds = number / 1000.0 if number > 1_000_000_000_000 else number
        return datetime.fromtimestamp(seconds, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str) and value.strip():
        text = value.strip()
        if text.endswith("Z"):
            return text
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            return text
    return None


def mask_email(value: Any) -> str:
    text = str(value or "unknown")
    at = text.find("@")
    if at <= 1:
        return text
    return f"{text[:1]}***{text[at:]}"


def token_expires_at(tokens: Mapping[str, Any]) -> float:
    for key in ("expires_at", "expiresAt"):
        number = number_or_none(tokens.get(key))
        if number and number > 0:
            return number
    for token_key in ("access_token", "id_token"):
        claims = jwt_claims(str(tokens.get(token_key) or ""))
        number = number_or_none(claims.get("exp"))
        if number and number > 0:
            return number
    return 0.0


def account_id_from_tokens(tokens: Mapping[str, Any]) -> str:
    for key in ("account_id", "accountId", "chatgpt_account_id", "chatgptAccountId"):
        value = text_value(tokens, key)
        if value:
            return value
    for token_key in ("id_token", "access_token"):
        claims = jwt_claims(str(tokens.get(token_key) or ""))
        for key in ("account_id", "accountId", "chatgpt_account_id", "chatgptAccountId", "https://api.openai.com/auth/account_id"):
            value = text_value(claims, key)
            if value:
                return value
        auth_claims = as_record(claims.get("https://api.openai.com/auth"))
        for key in ("chatgpt_account_id", "account_id", "accountId"):
            value = text_value(auth_claims, key)
            if value:
                return value
    return ""


def jwt_claims(token: str) -> Dict[str, Any]:
    parts = str(token or "").split(".")
    if len(parts) < 2:
        return {}
    payload = parts[1] + "=" * (-len(parts[1]) % 4)
    try:
        decoded = base64.urlsafe_b64decode(payload.encode("ascii"))
        value = json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}
    return value if isinstance(value, dict) else {}


def as_record(value: Any) -> Dict[str, Any]:
    return value if isinstance(value, dict) else {}


def text_value(mapping: Mapping[str, Any], key: str) -> str:
    if not isinstance(mapping, Mapping):
        return ""
    return str(mapping.get(key) or "").strip()


def number_or_none(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    if number != number:
        return None
    return number


def _parse_json_line(line: str) -> Dict[str, Any]:
    try:
        value = json.loads(line)
    except json.JSONDecodeError:
        return {}
    return value if isinstance(value, dict) else {}


def _server_request_default_response(method: str) -> Dict[str, Any]:
    if method.endswith("approval/request") or "approval" in method.lower():
        return {"approved": False}
    if "user_input" in method.lower() or "elicitation" in method.lower():
        return {"cancelled": True}
    return {}


if __name__ == "__main__":
    raise SystemExit(main())
