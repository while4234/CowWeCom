#!/usr/bin/env python3
"""
Codex CLI image broker for CowWechat.

Reads the standard image broker JSON payload from stdin, asks the logged-in
Codex CLI runtime to use its built-in image_gen tool, then copies the generated
bitmap from CODEX_HOME/generated_images into the requested output_dir.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp"}
GENERATED_PATH_RE = re.compile(
    r"([A-Za-z]:\\[^\r\n\"<>|]*?generated_images[^\r\n\"<>|]*?\.(?:png|jpg|jpeg|webp))",
    re.IGNORECASE,
)


def main() -> int:
    try:
        payload = _read_payload()
        prompt = str(payload.get("prompt") or "").strip()
        if not prompt:
            return _fail("missing prompt")

        output_dir = Path(str(payload.get("output_dir") or os.getcwd())).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)
        input_images = _normalize_images(payload.get("image_url"))

        codex_home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").resolve()
        marker = time.time()
        known_images = _collect_generated_images(codex_home)

        with tempfile.TemporaryDirectory(prefix="cow-codex-image-broker-") as tmp:
            last_message = Path(tmp) / "last_message.txt"
            cmd = _build_codex_command(last_message, input_images)
            broker_prompt = _build_codex_prompt(payload, input_images)
            print(f"[codex-broker] Using Codex CLI: {cmd[0]}", file=sys.stderr)
            result = subprocess.run(
                cmd,
                input=broker_prompt,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=_timeout_seconds(),
            )
            if result.returncode != 0:
                return _fail(
                    "codex exec failed with code {}: {}".format(
                        result.returncode,
                        _summarize_text(result.stderr or result.stdout),
                    )
                )

            generated = _find_generated_image(
                codex_home=codex_home,
                known_images=known_images,
                marker=marker,
                text="\n".join([
                    result.stdout or "",
                    result.stderr or "",
                    _read_text(last_message),
                ]),
            )
            if not generated:
                return _fail("Codex finished but no new generated image file was found")

            destination = output_dir / _result_filename(generated)
            shutil.copy2(generated, destination)
            print(json.dumps({"images": [{"url": str(destination)}]}, ensure_ascii=False))
            return 0
    except subprocess.TimeoutExpired:
        return _fail("codex exec timed out after {} seconds".format(_timeout_seconds()))
    except Exception as exc:
        return _fail(str(exc))


def _read_payload() -> dict[str, Any]:
    raw = sys.stdin.read()
    if not raw.strip():
        raise ValueError("missing stdin JSON payload")
    value = json.loads(raw)
    if not isinstance(value, dict):
        raise ValueError("stdin payload must be a JSON object")
    return value


def _normalize_images(value: Any) -> list[str]:
    if not value:
        return []
    items = value if isinstance(value, list) else [value]
    images: list[str] = []
    for item in items:
        text = str(item or "").strip()
        if not text:
            continue
        if _is_url(text):
            images.append(text)
            continue
        path = Path(text).expanduser()
        if not path.exists():
            raise FileNotFoundError(f"input image not found: {text}")
        images.append(str(path.resolve()))
    return images


def _is_url(value: str) -> bool:
    return value.lower().startswith(("http://", "https://"))


def _build_codex_command(last_message: Path, input_images: list[str]) -> list[str]:
    cmd = _codex_command_base()
    cmd.extend([
        "exec",
        "--skip-git-repo-check",
        "--ignore-user-config",
        "--sandbox",
        os.environ.get("CODEX_IMAGE_BROKER_SANDBOX", "read-only"),
        "--output-last-message",
        str(last_message),
    ])

    model = os.environ.get("CODEX_IMAGE_BROKER_MODEL", "").strip()
    if model:
        cmd.extend(["--model", model])

    for image in input_images:
        if not _is_url(image):
            cmd.extend(["--image", image])

    cmd.append("-")
    return cmd


def _codex_command_base() -> list[str]:
    command_json = os.environ.get("CODEX_IMAGE_BROKER_CODEX_COMMAND_JSON", "").strip()
    if command_json:
        parsed = json.loads(command_json)
        if not isinstance(parsed, list) or not parsed:
            raise ValueError("CODEX_IMAGE_BROKER_CODEX_COMMAND_JSON must be a non-empty JSON list")
        return [str(part) for part in parsed]

    configured_binary = (
        os.environ.get("CODEX_IMAGE_BROKER_CODEX_BINARY")
        or os.environ.get("CODEX_CLI_BINARY")
    )
    if configured_binary:
        return [configured_binary]

    binary = _find_codex_binary()
    if not binary:
        raise RuntimeError(
            "No Codex CLI binary found. Set CODEX_IMAGE_BROKER_CODEX_BINARY "
            "or CODEX_IMAGE_BROKER_CODEX_COMMAND_JSON."
        )
    return [str(binary)]


def _find_codex_binary(candidates: list[Path] | None = None) -> Path | None:
    seen: set[str] = set()
    for candidate in candidates or _candidate_codex_binaries():
        try:
            path = candidate.expanduser().resolve()
        except OSError:
            continue
        key = str(path).lower()
        if key in seen:
            continue
        seen.add(key)
        if _is_runnable_codex_binary(path):
            return path
    return None


def _candidate_codex_binaries() -> list[Path]:
    home = Path.home()
    candidates = [
        home
        / ".openclaw"
        / "extensions"
        / "codex"
        / "node_modules"
        / "@openai"
        / "codex-win32-x64"
        / "vendor"
        / "x86_64-pc-windows-msvc"
        / "codex"
        / "codex.exe",
    ]
    codexmobile_binary = os.environ.get("CODEXMOBILE_CODEX_BINARY")
    if codexmobile_binary:
        candidates.append(Path(codexmobile_binary))
    candidates.append(home / ".codex" / ".sandbox-bin" / "codex.exe")
    which = shutil.which("codex")
    if which:
        candidates.append(Path(which))
    return candidates


def _is_runnable_codex_binary(path: Path) -> bool:
    if not path.exists():
        return False
    try:
        result = subprocess.run(
            [str(path), "--version"],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=5,
        )
        return result.returncode == 0
    except Exception:
        return False


def _build_codex_prompt(payload: dict[str, Any], input_images: list[str]) -> str:
    lines = [
        "You are the Codex-side image broker for CowWechat.",
        "Use the built-in image_gen tool exactly once to produce the requested bitmap image.",
        "Do not use external image APIs, provider SDKs, or intermediary image endpoints.",
        "The caller will copy the output from CODEX_HOME/generated_images after this run.",
        "",
        "User request:",
        str(payload.get("prompt") or "").strip(),
    ]

    attached = [path for path in input_images if not _is_url(path)]
    urls = [path for path in input_images if _is_url(path)]
    if attached:
        lines.extend([
            "",
            "Attached input images are provided through Codex CLI --image arguments.",
            "For image editing, preserve identity, layout, and unchanged regions unless the user explicitly asks otherwise.",
        ])
    if urls:
        lines.extend([
            "",
            "Input image URLs:",
            *urls,
        ])

    for key in ("quality", "size", "aspect_ratio"):
        value = payload.get(key)
        if value:
            lines.append(f"{key}: {value}")

    lines.extend([
        "",
        "Return a short plain-text completion message only after image_gen finishes.",
    ])
    return "\n".join(lines)


def _timeout_seconds() -> int:
    raw = os.environ.get("CODEX_IMAGE_BROKER_TIMEOUT") or os.environ.get("SKILL_IMAGE_GENERATION_BROKER_TIMEOUT") or "900"
    return max(int(raw), 1)


def _collect_generated_images(codex_home: Path) -> set[Path]:
    root = codex_home / "generated_images"
    if not root.exists():
        return set()
    return {path.resolve() for path in root.rglob("*") if _is_image_file(path)}


def _find_generated_image(
    *,
    codex_home: Path,
    known_images: set[Path],
    marker: float,
    text: str,
) -> Path | None:
    for path in _paths_from_text(text):
        if path.exists() and _is_image_file(path):
            return path.resolve()

    deadline = time.time() + float(os.environ.get("CODEX_IMAGE_BROKER_SCAN_TIMEOUT", "20"))
    while time.time() <= deadline:
        candidates = [
            path.resolve()
            for path in (codex_home / "generated_images").rglob("*")
            if _is_image_file(path)
            and path.resolve() not in known_images
            and path.stat().st_mtime >= marker - 2
        ]
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
        time.sleep(0.5)
    return None


def _paths_from_text(text: str) -> list[Path]:
    paths: list[Path] = []
    for match in GENERATED_PATH_RE.finditer(text or ""):
        candidate = Path(match.group(1).strip())
        if candidate not in paths:
            paths.append(candidate)
    return paths


def _is_image_file(path: Path) -> bool:
    try:
        return path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS
    except OSError:
        return False


def _result_filename(source: Path) -> str:
    suffix = source.suffix.lower() if source.suffix.lower() in IMAGE_EXTENSIONS else ".png"
    return f"codex-image-{uuid.uuid4().hex[:12]}{suffix}"


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _summarize_text(text: str, max_chars: int = 800) -> str:
    value = " ".join(str(text or "").split())
    return value[-max_chars:]


def _fail(message: str) -> int:
    print(json.dumps({"error": message}, ensure_ascii=False))
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
