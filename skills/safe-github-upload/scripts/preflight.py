#!/usr/bin/env python3
"""Preflight guard for safe CowWechat GitHub uploads."""

from __future__ import annotations

import argparse
import fnmatch
import json
import os
import re
import subprocess
from pathlib import Path
from typing import Iterable


ALLOWLIST = {
    ".env.example",
    ".env.sample",
    "knowledge_backend/indexes/kb.sqlite",
}

REQUIRED_IGNORE_RULES = [
    "config.json",
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "credentials*.json",
    "token*.json",
    "cookies*.json",
    "session*.json",
    ".weixin_cow_credentials.json",
    ".codex/",
    ".playwright-mcp/",
]

PROTECTED_PATTERNS = [
    "config.json",
    "config.json.backup.*",
    ".env",
    ".env.*",
    "*.key",
    "*.pem",
    "credentials*.json",
    "token*.json",
    "cookies*.json",
    "session*.json",
    "service-account*.json",
    "id_rsa",
    "id_ed25519",
    ".weixin_cow_credentials.json",
    "QR.png",
    "*.log",
    "logs/*",
    "secrets/*",
    "workspace/*",
    "tmp/*",
    ".codex/*",
    ".playwright-mcp/*",
    ".venv/*",
    "venv*/*",
    "node_modules/*",
    "local/*",
    "knowledge_backend/indexes/*.sqlite",
    "knowledge_backend/indexes/*.sqlite3",
    "*.sqlite",
    "*.sqlite3",
    "*.db",
]

SECRET_PATTERNS = [
    re.compile(r"ghp_[A-Za-z0-9_]{20,}"),
    re.compile(r"github_pat_[A-Za-z0-9_]{20,}"),
    re.compile(r"sk-[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN (?:RSA |OPENSSH |EC |DSA )?PRIVATE KEY-----"),
]


def run_git(root: Path, args: Iterable[str], check: bool = False) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=str(root),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if check and result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result


def find_git_root(start: Path) -> Path:
    result = run_git(start, ["rev-parse", "--show-toplevel"])
    if result.returncode != 0:
        raise RuntimeError("not inside a Git repository")
    return Path(result.stdout.strip()).resolve()


def normalize_path(path: str) -> str:
    return path.strip().strip('"').replace("\\", "/")


def parse_status(output: str) -> list[dict[str, str]]:
    entries: list[dict[str, str]] = []
    for line in output.splitlines():
        if not line:
            continue
        status = line[:2]
        path = normalize_path(line[3:])
        if " -> " in path:
            path = normalize_path(path.split(" -> ", 1)[1])
        entries.append({"status": status, "path": path})
    return entries


def is_protected(path: str) -> bool:
    normalized = normalize_path(path)
    lower = normalized.lower()
    if lower in ALLOWLIST:
        return False
    if lower.startswith("knowledge_backend/originals/"):
        return False
    if lower.startswith("knowledge_backend/derived/"):
        return False
    if lower.startswith("knowledge_backend/reports/"):
        return False
    parts = lower.split("/")
    protected_dirs = {".codex", ".playwright-mcp", ".venv", "node_modules", "logs", "secrets"}
    if any(part in protected_dirs for part in parts):
        return True
    return any(fnmatch.fnmatch(lower, pattern.lower()) for pattern in PROTECTED_PATTERNS)


def read_ignore_rules(root: Path) -> set[str]:
    ignore_file = root / ".gitignore"
    if not ignore_file.exists():
        return set()
    rules: set[str] = set()
    for raw in ignore_file.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#"):
            rules.add(line)
    return rules


def staged_files(root: Path) -> list[str]:
    result = run_git(root, ["diff", "--cached", "--name-only"])
    return [normalize_path(line) for line in result.stdout.splitlines() if line.strip()]


def staged_name_status(root: Path) -> list[dict[str, str]]:
    result = run_git(root, ["diff", "--cached", "--name-status"])
    entries: list[dict[str, str]] = []
    for line in result.stdout.splitlines():
        if not line.strip():
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        status = parts[0].strip()
        path = normalize_path(parts[-1])
        entries.append({"status": status, "path": path})
    return entries


def scan_staged_secrets(root: Path, paths: Iterable[str]) -> list[dict[str, str]]:
    hits: list[dict[str, str]] = []
    for rel in paths:
        target = root / rel
        if not target.is_file():
            continue
        try:
            data = target.read_bytes()
        except OSError:
            continue
        if b"\x00" in data or len(data) > 2_000_000:
            continue
        text = data.decode("utf-8", errors="ignore")
        for pattern in SECRET_PATTERNS:
            if pattern.search(text):
                hits.append({"path": rel, "pattern": pattern.pattern})
                break
    return hits


def build_report(root: Path) -> tuple[dict, bool]:
    status_result = run_git(root, ["status", "--porcelain=v1", "--untracked-files=all"])
    status_entries = parse_status(status_result.stdout)
    staged_entries = staged_name_status(root)
    staged = [entry["path"] for entry in staged_entries]
    ignore_rules = read_ignore_rules(root)
    missing_ignore_rules = [rule for rule in REQUIRED_IGNORE_RULES if rule not in ignore_rules]
    protected_staged = [
        entry["path"]
        for entry in staged_entries
        if entry["status"] != "D" and is_protected(entry["path"])
    ]
    protected_changed = [entry["path"] for entry in status_entries if is_protected(entry["path"])]
    secret_hits = scan_staged_secrets(root, staged)
    branch = run_git(root, ["branch", "--show-current"]).stdout.strip()
    status_header = run_git(root, ["status", "--short", "--branch"]).stdout.splitlines()[:1]
    remotes = run_git(root, ["remote", "-v"]).stdout.splitlines()
    ignored = run_git(root, ["status", "--porcelain=v1", "--ignored=matching", "--untracked-files=all"]).stdout
    ignored_paths = [
        normalize_path(line[3:])
        for line in ignored.splitlines()
        if line.startswith("!! ")
    ]
    report = {
        "root": str(root),
        "branch": branch,
        "status_header": status_header[0] if status_header else "",
        "changed_files": status_entries,
        "staged_files": staged,
        "protected_changed": sorted(set(protected_changed)),
        "protected_staged": sorted(set(protected_staged)),
        "secret_hits": secret_hits,
        "missing_ignore_rules": missing_ignore_rules,
        "ignored_paths_count": len(ignored_paths),
        "ignored_paths_sample": ignored_paths[:20],
        "remotes": remotes,
    }
    blocked = bool(protected_staged or secret_hits or not ignore_rules)
    return report, blocked


def print_markdown(report: dict, blocked: bool) -> None:
    state = "BLOCKED" if blocked else "OK"
    print(f"# Safe GitHub Upload Preflight: {state}")
    print(f"- Root: {report['root']}")
    print(f"- Branch: {report['branch'] or '(detached)'}")
    print(f"- Status: {report['status_header']}")
    print(f"- Staged files: {len(report['staged_files'])}")
    print(f"- Changed files: {len(report['changed_files'])}")
    print(f"- Ignored files detected: {report['ignored_paths_count']}")
    if report["protected_staged"]:
        print("\n## Blocked Staged Protected Files")
        for path in report["protected_staged"]:
            print(f"- {path}")
    if report["secret_hits"]:
        print("\n## Blocked Staged Secret-Like Content")
        for hit in report["secret_hits"]:
            print(f"- {hit['path']} matched `{hit['pattern']}`")
    if report["protected_changed"]:
        print("\n## Protected/Runtime Files In Working Tree")
        for path in report["protected_changed"]:
            marker = " (staged)" if path in report["protected_staged"] else ""
            print(f"- {path}{marker}")
    if report["missing_ignore_rules"]:
        print("\n## Missing Recommended .gitignore Rules")
        for rule in report["missing_ignore_rules"]:
            print(f"- {rule}")
    if report["ignored_paths_sample"]:
        print("\n## Ignored Sample")
        for path in report["ignored_paths_sample"]:
            print(f"- {path}")
    print("\n## Remotes")
    for remote in report["remotes"]:
        safe_remote = re.sub(r"https://[^/@\s]+@", "https://<redacted>@", remote)
        print(f"- {safe_remote}")
    if blocked:
        print("\nDo not commit or push until blocked staged files are unstaged/removed.")
    else:
        print("\nPreflight did not find blocked staged files or obvious staged secrets.")


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit a CowWechat checkout before GitHub upload.")
    parser.add_argument("--root", default=os.getcwd(), help="Repository path or any path inside it.")
    parser.add_argument("--json", action="store_true", help="Print JSON instead of Markdown.")
    args = parser.parse_args()

    try:
        root = find_git_root(Path(args.root).resolve())
        report, blocked = build_report(root)
    except Exception as exc:  # pragma: no cover - defensive CLI boundary
        if args.json:
            print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        else:
            print(f"Safe GitHub Upload Preflight: ERROR\n- {exc}")
        return 2

    if args.json:
        report["status"] = "blocked" if blocked else "ok"
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print_markdown(report, blocked)
    return 2 if blocked else 0


if __name__ == "__main__":
    raise SystemExit(main())
