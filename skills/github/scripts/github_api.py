#!/usr/bin/env python3
"""Small GitHub REST helper using env vars or Git Credential Manager."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import shutil


API_ROOT = "https://api.github.com"


def resolve_token() -> str:
    for name in ("GITHUB_TOKEN", "GH_TOKEN"):
        value = os.environ.get(name, "").strip()
        if value:
            return value

    proc = subprocess.run(
        ["git", "credential", "fill"],
        input="protocol=https\nhost=github.com\n\n",
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.returncode != 0:
        return ""

    fields: dict[str, str] = {}
    for line in proc.stdout.splitlines():
        if "=" not in line:
            continue
        key, value = line.split("=", 1)
        fields[key] = value

    return fields.get("password", "").strip()


def build_url(endpoint: str) -> str:
    if endpoint.startswith("https://api.github.com/"):
        return endpoint
    if not endpoint.startswith("/"):
        endpoint = "/" + endpoint
    return API_ROOT + endpoint


def parse_body(data_json: str | None) -> bytes | None:
    if not data_json:
        return None
    try:
        parsed = json.loads(data_json)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid --data-json: {exc}") from exc
    return json.dumps(parsed, ensure_ascii=False).encode("utf-8")


def request(method: str, endpoint: str, data_json: str | None) -> tuple[int, bytes]:
    token = resolve_token()
    if not token:
        raise SystemExit(
            "No GitHub token found. Set GITHUB_TOKEN/GH_TOKEN or sign in through Git Credential Manager."
        )

    body = parse_body(data_json)
    curl = shutil.which("curl.exe") or shutil.which("curl")
    if not curl:
        raise SystemExit("curl is required but was not found on PATH.")

    args = [
        curl,
        "-sS",
        "-L",
        "-X",
        method.upper(),
        "-H",
        f"Authorization: Bearer {token}",
        "-H",
        "Accept: application/vnd.github+json",
        "-H",
        "X-GitHub-Api-Version: 2022-11-28",
        "-H",
        "User-Agent: cowwechat-github-skill",
        "-w",
        "\n%{http_code}",
    ]
    if body is not None:
        args.extend(["-H", "Content-Type: application/json", "--data-binary", "@-"])
    args.append(build_url(endpoint))

    proc = subprocess.run(
        args,
        input=body,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )
    if proc.stderr:
        sys.stderr.buffer.write(proc.stderr)

    payload, _, status_raw = proc.stdout.rpartition(b"\n")
    try:
        status = int(status_raw)
    except ValueError:
        status = 0
        payload = proc.stdout

    if proc.returncode != 0:
        raise SystemExit(proc.returncode)
    if status < 200 or status >= 300:
        sys.stderr.write(f"GitHub API returned HTTP {status}\n")
        pretty_print(payload, stream=sys.stderr)
        raise SystemExit(1)

    return status, payload


def pretty_print(payload: bytes, stream=sys.stdout) -> None:
    if not payload:
        return
    text = payload.decode("utf-8", errors="replace")
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        stream.write(text)
        if not text.endswith("\n"):
            stream.write("\n")
        return
    stream.write(json.dumps(parsed, ensure_ascii=False, indent=2))
    stream.write("\n")


def main() -> int:
    parser = argparse.ArgumentParser(description="Call the GitHub REST API.")
    parser.add_argument("--method", default="GET", help="HTTP method, default: GET")
    parser.add_argument("--endpoint", help="API endpoint such as /user")
    parser.add_argument("--data-json", help="JSON request body for POST/PATCH/PUT")
    parser.add_argument(
        "--check-auth",
        action="store_true",
        help="Only check whether a local token can be resolved; do not call GitHub.",
    )
    args = parser.parse_args()

    if args.check_auth:
        print("token_found=" + str(bool(resolve_token())).lower())
        return 0

    if not args.endpoint:
        parser.error("--endpoint is required unless --check-auth is used")

    _status, payload = request(args.method, args.endpoint, args.data_json)
    pretty_print(payload)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
