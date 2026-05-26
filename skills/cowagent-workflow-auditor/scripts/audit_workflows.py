#!/usr/bin/env python3
"""Privacy-safe CowAgent/CowWechat repeated workflow auditor."""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path


SENSITIVE_NAMES = {
    ".env",
    "config.json",
    "cookies.json",
    "credentials.json",
    "token.json",
}

TOOL_LINE_RE = re.compile(r"🔧\s+([A-Za-z_][\w-]*)\(")
TMP_SCRIPT_RE = re.compile(r"write\(path=([^,\)]*?(?:tmp|workspace)[^,\)]*?\.py)", re.IGNORECASE)
BASH_TMP_RE = re.compile(r"bash\(command=(?:python|py|powershell|pwsh)[^,\)]*?(?:tmp|workspace)[^,\)]*", re.IGNORECASE)
SKILL_READ_RE = re.compile(r"read\(path=([^,\)]*?skills[\\/][^,\)]*?SKILL\.md)", re.IGNORECASE)
WORKSPACE_REPEAT_RE = re.compile(r"^(.+?)(?:\d+)?$")


def safe_read_lines(path: Path, max_bytes: int) -> list[str]:
    if not path.exists() or not path.is_file():
        return []
    if path.name.lower() in SENSITIVE_NAMES:
        return []
    size = path.stat().st_size
    with path.open("rb") as fh:
        if size > max_bytes:
            fh.seek(max(0, size - max_bytes))
        data = fh.read(max_bytes)
    return data.decode("utf-8", errors="replace").splitlines()


def normalize_path_label(raw: str) -> str:
    return raw.strip().strip('"\'').replace("/", "\\")


def extract_skill_name(raw: str) -> str:
    normalized = normalize_path_label(raw)
    parts = [p for p in normalized.split("\\") if p]
    lowered = [p.lower() for p in parts]
    try:
        idx = lowered.index("skills")
        return parts[idx + 1]
    except Exception:
        return normalized


def scan_log(path: Path, max_bytes: int) -> dict:
    lines = safe_read_lines(path, max_bytes)
    tools: Counter[str] = Counter()
    tmp_scripts: Counter[str] = Counter()
    bash_tmp_count = 0
    skill_reads: Counter[str] = Counter()

    for line in lines:
        for match in TOOL_LINE_RE.finditer(line):
            tools[match.group(1)] += 1
        for match in TMP_SCRIPT_RE.finditer(line):
            tmp_scripts[normalize_path_label(match.group(1))] += 1
        if BASH_TMP_RE.search(line):
            bash_tmp_count += 1
        for match in SKILL_READ_RE.finditer(line):
            skill_reads[extract_skill_name(match.group(1))] += 1

    return {
        "path": str(path),
        "lines_scanned": len(lines),
        "tool_counts": tools.most_common(30),
        "tmp_script_writes": tmp_scripts.most_common(30),
        "bash_tmp_invocations": bash_tmp_count,
        "skill_reads": skill_reads.most_common(30),
    }


def scan_artifact_dirs(root: Path, max_depth: int = 2) -> list[dict]:
    if not root.exists() or not root.is_dir():
        return []
    entries: list[dict] = []
    for child in root.iterdir():
        if not child.is_dir():
            continue
        prefix = WORKSPACE_REPEAT_RE.match(child.name).group(1)
        files = []
        for item in child.rglob("*"):
            try:
                rel = item.relative_to(child)
            except ValueError:
                continue
            if len(rel.parts) > max_depth:
                continue
            if item.is_file() and item.name.lower() not in SENSITIVE_NAMES:
                files.append(str(rel))
        entries.append(
            {
                "name": child.name,
                "repeat_prefix": prefix,
                "file_count_sample": len(files),
                "sample_files": files[:12],
                "last_write": datetime.fromtimestamp(
                    child.stat().st_mtime, tz=timezone.utc
                ).isoformat(),
            }
        )
    return sorted(entries, key=lambda item: item["last_write"], reverse=True)


def group_repeat_dirs(entries: list[dict]) -> list[dict]:
    grouped: dict[str, list[dict]] = {}
    for entry in entries:
        grouped.setdefault(entry["repeat_prefix"], []).append(entry)
    result = []
    for prefix, items in grouped.items():
        if len(items) < 2:
            continue
        result.append(
            {
                "prefix": prefix,
                "count": len(items),
                "examples": [item["name"] for item in items[:8]],
                "sample_files": items[0].get("sample_files", []),
            }
        )
    return sorted(result, key=lambda item: item["count"], reverse=True)


def scan_skills(root: Path) -> list[str]:
    skills_dir = root / "skills"
    if not skills_dir.exists():
        return []
    return sorted(p.name for p in skills_dir.iterdir() if p.is_dir() and (p / "SKILL.md").exists())


def scan_plugins(root: Path) -> list[str]:
    plugins_dir = root / "plugins"
    if not plugins_dir.exists():
        return []
    return sorted(p.name for p in plugins_dir.iterdir() if p.is_dir())


def main() -> int:
    parser = argparse.ArgumentParser(description="Audit repeated CowAgent/CowWechat workflows.")
    parser.add_argument("--project-root", default=".", help="CowWechat project root.")
    parser.add_argument("--workspace", default="", help="Runtime workspace, usually ~/cow.")
    parser.add_argument("--max-log-bytes", type=int, default=2_000_000)
    parser.add_argument("--json-out", default="", help="Optional output JSON path.")
    args = parser.parse_args()

    project = Path(args.project_root).expanduser().resolve()
    workspace = Path(args.workspace).expanduser().resolve() if args.workspace else None

    log_paths = [project / "run.log", project / "nohup.out"]
    if workspace:
        log_paths.extend([workspace / "run.log", workspace / "nohup.out"])

    logs = [scan_log(path, args.max_log_bytes) for path in log_paths if path.exists()]
    artifact_roots = [project / "tmp", project / "workspace"]
    if workspace:
        artifact_roots.extend([workspace / "tmp", workspace / "workspace"])

    artifact_entries = []
    for root in artifact_roots:
        artifact_entries.extend(scan_artifact_dirs(root))

    report = {
        "project_root": str(project),
        "workspace": str(workspace) if workspace else "",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "logs": logs,
        "repeat_artifact_dirs": group_repeat_dirs(artifact_entries),
        "skills": scan_skills(project),
        "plugins": scan_plugins(project),
    }

    text = json.dumps(report, ensure_ascii=False, indent=2)
    if args.json_out:
        out = Path(args.json_out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(text + "\n", encoding="utf-8")
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
