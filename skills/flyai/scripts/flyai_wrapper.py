#!/usr/bin/env python3
"""Windows-safe wrapper for the FlyAI CLI.

The FlyAI CLI can print valid JSON to stdout and then exit nonzero on Windows
because of a known libuv assertion. This wrapper preserves the usable JSON
payload while making the process result explicit for callers.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
from typing import Any, Callable


DEFAULT_TIMEOUT_SECONDS = 120.0
KNOWN_LIBUV_MARKERS = (
    "Assertion failed: !(handle->flags & UV_HANDLE_CLOSING)",
    "UV_HANDLE_CLOSING",
    "libuv",
)
SECRET_FIELD_NAMES = {
    "api_key",
    "apikey",
    "authorization",
    "flyai_api_key",
    "password",
    "refresh_token",
    "secret",
    "token",
    "x-api-key",
}
SECRET_TEXT_REPLACEMENTS = (
    (
        re.compile(r"(?i)\b(AUTHORIZATION)\s*[:=]\s*(?:Bearer\s+)?[A-Za-z0-9._~+/\-]+=*"),
        r"\1=<redacted>",
    ),
    (
        re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/\-]+=*"),
        r"\1<redacted>",
    ),
    (
        re.compile(
            r"(?i)\b(FLYAI_API_KEY|API_KEY|X-API-KEY|ACCESS_TOKEN|REFRESH_TOKEN|TOKEN|SECRET|PASSWORD)"
            r"\s*[:=]\s*([^\s,;]+)"
        ),
        r"\1=<redacted>",
    ),
)


Runner = Callable[..., subprocess.CompletedProcess]


def _redact_text(value: str) -> str:
    redacted = value
    for pattern, replacement in SECRET_TEXT_REPLACEMENTS:
        redacted = pattern.sub(replacement, redacted)
    return redacted


def _redact_json(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if str(key).lower() in SECRET_FIELD_NAMES:
                result[key] = "<redacted>"
            else:
                result[key] = _redact_json(item)
        return result
    if isinstance(value, list):
        return [_redact_json(item) for item in value]
    if isinstance(value, str):
        return _redact_text(value)
    return value


def _truncate(value: str, limit: int = 4000) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + "...<truncated>"


def _parse_json_from_stdout(stdout: str) -> Any | None:
    text = stdout.strip("\ufeff \t\r\n")
    if not text:
        return None

    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass

    decoder = json.JSONDecoder()
    for index, char in enumerate(text):
        if char not in "{[":
            continue
        try:
            parsed, _end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        return parsed

    return None


def _is_known_windows_libuv_failure(stderr: str) -> bool:
    return all(marker in stderr for marker in ("Assertion failed", "UV_HANDLE_CLOSING")) or any(
        marker in stderr for marker in KNOWN_LIBUV_MARKERS
    )


def _payload_status_is_success(payload: Any) -> bool:
    if isinstance(payload, dict) and "status" in payload:
        return payload.get("status") == 0
    return True


def _wrapper_metadata(
    *,
    exit_code: int | None,
    warnings: list[str],
    stderr: str | None = None,
) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "flyai_exit_code": exit_code,
        "warnings": warnings,
    }
    if stderr:
        metadata["stderr"] = _truncate(_redact_text(stderr.strip()))
    return metadata


def _build_parsed_response(proc: subprocess.CompletedProcess, payload: Any) -> tuple[dict[str, Any], int]:
    warnings: list[str] = []
    known_libuv_failure = proc.returncode != 0 and _is_known_windows_libuv_failure(proc.stderr or "")
    if known_libuv_failure:
        warnings.append(
            "flyai CLI exited nonzero after producing JSON because of the known Windows libuv assertion; "
            "using stdout JSON."
        )
    elif proc.returncode != 0:
        warnings.append("flyai CLI exited nonzero after producing JSON; using stdout JSON with caution.")

    payload_success = _payload_status_is_success(payload)
    ok = payload_success and (proc.returncode == 0 or known_libuv_failure)
    if payload_success and proc.returncode != 0 and not known_libuv_failure:
        wrapper_exit_code = proc.returncode
    else:
        wrapper_exit_code = 0 if ok else 1

    response = {
        "ok": ok,
        "result": _redact_json(payload),
        "_flyai_wrapper": _wrapper_metadata(exit_code=proc.returncode, warnings=warnings),
    }
    return response, wrapper_exit_code


def _build_error_response(
    *,
    message: str,
    exit_code: int | None,
    stderr: str | None = None,
    stdout: str | None = None,
) -> tuple[dict[str, Any], int]:
    details: dict[str, Any] = {}
    if stdout:
        details["stdout"] = _truncate(_redact_text(stdout.strip()))
    response = {
        "ok": False,
        "error": message,
        "_flyai_wrapper": _wrapper_metadata(
            exit_code=exit_code,
            warnings=[],
            stderr=stderr,
        ),
    }
    if details:
        response["details"] = details
    return response, exit_code or 1


def _resolve_timeout() -> float:
    raw = os.environ.get("FLYAI_WRAPPER_TIMEOUT_SECONDS", "").strip()
    if not raw:
        return DEFAULT_TIMEOUT_SECONDS
    try:
        timeout = float(raw)
    except ValueError:
        return DEFAULT_TIMEOUT_SECONDS
    return timeout if timeout > 0 else DEFAULT_TIMEOUT_SECONDS


def run_wrapper(
    flyai_args: list[str],
    *,
    runner: Runner = subprocess.run,
    flyai_bin: str | None = None,
    timeout: float | None = None,
) -> tuple[dict[str, Any], int]:
    executable = flyai_bin or os.environ.get("FLYAI_BIN") or shutil.which("flyai")
    if not executable:
        return _build_error_response(
            message="flyai CLI was not found on PATH. Install @fly-ai/flyai-cli or set FLYAI_BIN.",
            exit_code=127,
        )

    try:
        proc = runner(
            [executable, *flyai_args],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=False,
            timeout=timeout if timeout is not None else _resolve_timeout(),
        )
    except FileNotFoundError:
        return _build_error_response(
            message="flyai CLI executable was not found.",
            exit_code=127,
        )
    except subprocess.TimeoutExpired as exc:
        stdout = exc.stdout if isinstance(exc.stdout, str) else ""
        stderr = exc.stderr if isinstance(exc.stderr, str) else ""
        return _build_error_response(
            message="flyai CLI timed out.",
            exit_code=124,
            stderr=stderr,
            stdout=stdout,
        )

    payload = _parse_json_from_stdout(proc.stdout or "")
    if payload is not None:
        return _build_parsed_response(proc, payload)

    return _build_error_response(
        message="flyai CLI did not produce valid JSON on stdout.",
        exit_code=proc.returncode or 1,
        stderr=proc.stderr or "",
        stdout=proc.stdout or "",
    )


def main(argv: list[str] | None = None) -> int:
    flyai_args = sys.argv[1:] if argv is None else argv
    response, exit_code = run_wrapper(flyai_args)
    sys.stdout.write(json.dumps(response, ensure_ascii=False, separators=(",", ":")))
    sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
