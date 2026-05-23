#!/usr/bin/env python3
"""Static pre-install guard for CowWechat community skills."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from dataclasses import asdict, dataclass
from pathlib import Path


TEXT_EXTENSIONS = {
    ".md",
    ".txt",
    ".py",
    ".ps1",
    ".sh",
    ".bash",
    ".bat",
    ".cmd",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".js",
    ".ts",
}

MAX_FILE_BYTES = 512_000
MAX_TOTAL_BYTES = 3_000_000


@dataclass
class Finding:
    severity: str
    code: str
    file: str
    line: int
    message: str
    snippet: str


RULES: list[tuple[str, str, str, re.Pattern[str]]] = [
    (
        "block",
        "PROMPT_INJECTION",
        "Instruction attempts to override higher-priority instructions.",
        re.compile(r"ignore (all )?(previous|prior|above|system|developer) instructions|disregard (all )?(previous|prior|above) instructions|你现在.*不受.*限制|忽略.*(系统|开发者|之前|以上).*指令", re.I),
    ),
    (
        "block",
        "SECRET_EXFILTRATION",
        "Credential or secret capture/exfiltration behavior.",
        re.compile(r"(send|upload|post|exfiltrate|leak|print|dump).{0,80}(secret|token|cookie|api[_-]?key|ssh|id_rsa|wallet|seed|\.env|credential)|上传.{0,40}(密钥|token|cookie|凭证|钱包|助记词|\.env)", re.I),
    ),
    (
        "block",
        "DESTRUCTIVE_COMMAND",
        "Destructive filesystem or git command.",
        re.compile(r"rm\s+-rf\s+[/~$%]|Remove-Item\s+.*(-Recurse).*(-Force)|format\s+[a-z]:|git\s+reset\s+--hard|chmod\s+-R\s+777\s+[/~$%]", re.I),
    ),
    (
        "block",
        "REMOTE_SHELL_EXEC",
        "Remote content is fetched and executed directly.",
        re.compile(r"(curl|wget|Invoke-WebRequest|iwr).{0,120}(\|\s*(sh|bash|powershell|pwsh|python)|iex|Invoke-Expression)", re.I),
    ),
    (
        "block",
        "BROWSER_PROFILE_SWEEP",
        "Code searches browser profiles or credential stores.",
        re.compile(r"(Chrome|Edge|Firefox|Login Data|Cookies|Local State|keychain|Credential Manager).{0,120}(walk|glob|rglob|copy|upload|read|open)", re.I),
    ),
    (
        "warn",
        "SUSPICIOUS_ENDPOINT",
        "Suspicious external endpoint; verify it is necessary.",
        re.compile(r"https?://(?:[^/\s]+\.)?(webhook\.site|ngrok\.io|trycloudflare\.com|pastebin\.com|requestbin|discord\.com/api/webhooks|telegram\.org/bot)|https?://\d{1,3}(?:\.\d{1,3}){3}", re.I),
    ),
    (
        "warn",
        "BROAD_FILE_ACCESS",
        "Broad home/root traversal or credential-adjacent path access.",
        re.compile(r"(os\.walk|Path\([^)]*home|rglob\(['\"]\*|glob\(['\"]\*\*/\*|/etc/passwd|~/.ssh|\.aws|\.config/gcloud)", re.I),
    ),
    (
        "warn",
        "POTENTIAL_SECRET",
        "Possible hardcoded secret or private key.",
        re.compile(r"(sk-[A-Za-z0-9_-]{20,}|AKIA[0-9A-Z]{16}|-----BEGIN (RSA |OPENSSH |EC )?PRIVATE KEY-----|api[_-]?key\s*[:=]\s*['\"][^'\"]{16,})", re.I),
    ),
]


def iter_text_files(root: Path) -> tuple[list[Path], list[str]]:
    diagnostics: list[str] = []
    files: list[Path] = []
    total = 0
    for path in root.rglob("*"):
        if path.is_dir():
            if path.name in {".git", "__pycache__", "node_modules", ".venv", "venv"}:
                continue
            continue
        if path.suffix.lower() not in TEXT_EXTENSIONS:
            continue
        try:
            size = path.stat().st_size
        except OSError as exc:
            diagnostics.append(f"stat failed for {path}: {exc}")
            continue
        if size > MAX_FILE_BYTES:
            diagnostics.append(f"skipped large file: {path}")
            continue
        total += size
        if total > MAX_TOTAL_BYTES:
            diagnostics.append("total scan size limit reached")
            break
        files.append(path)
    return files, diagnostics


def scan_file(root: Path, path: Path) -> list[Finding]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        rel = str(path.relative_to(root))
        return [Finding("warn", "READ_ERROR", rel, 0, f"Could not read file: {exc}", "")]

    findings: list[Finding] = []
    rel = str(path.relative_to(root))
    for line_no, line in enumerate(text.splitlines(), start=1):
        compact = line.strip()
        if not compact:
            continue
        if rel.replace("\\", "/") == "scripts/scan_skill.py" and ("re.compile(" in compact or "root.rglob(" in compact):
            continue
        for severity, code, message, pattern in RULES:
            if pattern.search(compact):
                findings.append(Finding(severity, code, rel, line_no, message, compact[:240]))
    return findings


def scan(root: Path) -> dict:
    if not root.exists():
        raise FileNotFoundError(str(root))
    if root.is_file():
        candidate_root = root.parent
    else:
        candidate_root = root

    files, diagnostics = iter_text_files(candidate_root)
    findings: list[Finding] = []
    for path in files:
        findings.extend(scan_file(candidate_root, path))

    blocking = [item for item in findings if item.severity == "block"]
    has_skill_md = (candidate_root / "SKILL.md").exists() or root.name.upper() == "SKILL.MD"
    digest = hashlib.sha256()
    for path in sorted(files):
        digest.update(str(path.relative_to(candidate_root)).encode("utf-8", errors="replace"))
        try:
            digest.update(path.read_bytes())
        except OSError:
            pass

    return {
        "path": str(candidate_root),
        "has_skill_md": has_skill_md,
        "file_count": len(files),
        "sha256": digest.hexdigest(),
        "blocked": bool(blocking) or not has_skill_md,
        "findings": [asdict(item) for item in findings],
        "diagnostics": diagnostics + ([] if has_skill_md else ["SKILL.md not found"]),
    }


def format_report(report: dict) -> str:
    lines = [
        f"Skill guard scan: {report['path']}",
        f"Files scanned: {report['file_count']}",
        f"SHA256: {report['sha256']}",
        "Result: BLOCKED" if report["blocked"] else "Result: PASS",
    ]
    if report["diagnostics"]:
        lines.append("Diagnostics:")
        lines.extend(f"- {item}" for item in report["diagnostics"])
    if report["findings"]:
        lines.append("Findings:")
        for item in report["findings"]:
            lines.append(f"- [{item['severity']}] {item['code']} {item['file']}:{item['line']} - {item['message']}")
            if item["snippet"]:
                lines.append(f"  {item['snippet']}")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="Scan a staged skill before installation.")
    parser.add_argument("path", help="Candidate skill directory or SKILL.md")
    parser.add_argument("--json", action="store_true", help="Print JSON report")
    args = parser.parse_args()
    try:
        report = scan(Path(args.path).expanduser().resolve())
    except Exception as exc:
        error = {"blocked": True, "error": str(exc)}
        if args.json:
            print(json.dumps(error, ensure_ascii=False, indent=2))
        else:
            print(f"Skill guard scan error: {exc}")
        return 1
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(format_report(report))
    return 2 if report["blocked"] else 0


if __name__ == "__main__":
    sys.exit(main())
