#!/usr/bin/env python3
"""Small ClawHub/OpenClaw skill discovery helper for CowWechat."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Iterable


@dataclass
class SkillLead:
    name: str
    source: str
    url: str
    downloads: str = ""
    stars: str = ""
    audit: str = ""
    note: str = ""


KNOWN_LEADS = [
    SkillLead(
        name="find-skills-skill",
        source="ClawHub",
        url="https://clawhub.ai/fangkelvin/find-skills-skill",
        downloads="47.6k",
        stars="123",
        audit="Pending",
        note="Popular skill-search workflow. Review and localize before use.",
    ),
    SkillLead(
        name="skill-search / dynamic-skills",
        source="OpenClaw Directory",
        url="https://openclawdir.com/skills/dynamic-skills-o9cg2m",
        downloads="",
        stars="0 votes",
        audit="Unknown",
        note="Dynamic loading workflow; not preferred for CowWechat because permanent local review is safer.",
    ),
]


@dataclass(frozen=True)
class CliCommand:
    label: str
    argv: list[str]


def _dedupe(values: Iterable[str]) -> list[str]:
    seen = set()
    unique = []
    for value in values:
        key = value.lower() if os.name == "nt" else value
        if key in seen:
            continue
        seen.add(key)
        unique.append(value)
    return unique


def _common_node_dirs() -> list[Path]:
    candidates: list[Path] = []
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "APPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        if env_name == "APPDATA":
            candidates.append(Path(base) / "npm")
        else:
            candidates.append(Path(base) / "nodejs")

    local_appdata = os.environ.get("LOCALAPPDATA")
    if local_appdata:
        winget_packages = Path(local_appdata) / "Microsoft" / "WinGet" / "Packages"
        if winget_packages.exists():
            candidates.extend(winget_packages.glob("OpenJS.NodeJS*/node-*"))

    return [path for path in candidates if path.exists()]


def find_executable(names: Iterable[str]) -> str | None:
    for name in names:
        found = shutil.which(name)
        if found:
            return found

    for directory in _common_node_dirs():
        for name in names:
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    return None


def _limit_args(sort: str) -> list[str]:
    return ["--limit", "10"] if sort else []


def resolve_clawhub_commands(query: str, sort: str) -> list[CliCommand]:
    suffix = ["search", query, *_limit_args(sort)]
    commands: list[CliCommand] = []

    clawhub = find_executable(("clawhub.cmd", "clawhub.exe", "clawhub"))
    if clawhub:
        commands.append(CliCommand(label="clawhub", argv=[clawhub, *suffix]))

    npx = find_executable(("npx.cmd", "npx.exe", "npx"))
    if npx:
        commands.append(CliCommand(label="npx clawhub", argv=[npx, "clawhub", *suffix]))

    seen = set()
    deduped = []
    for command in commands:
        key = tuple(command.argv)
        if key in seen:
            continue
        seen.add(key)
        deduped.append(command)
    return deduped


def run_clawhub(query: str, sort: str) -> str | None:
    commands = resolve_clawhub_commands(query, sort)
    if not commands:
        return None

    failures = []
    try:
        # Current clawhub CLI exposes vector search plus --limit. Ranking flags
        # vary between releases, so popularity sorting is handled by listing metadata.
        for command in commands:
            completed = subprocess.run(
                command.argv,
                check=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
            )
            if completed.returncode == 0:
                return completed.stdout.strip()
            reason = completed.stderr.strip() or completed.stdout.strip() or f"exit {completed.returncode}"
            failures.append(f"{command.label}: {reason}")
    except Exception as exc:
        failures.append(str(exc))
    return "clawhub_cli_error: " + " | ".join(_dedupe(failures))


def main() -> int:
    parser = argparse.ArgumentParser(description="Find community skills and known vetted leads.")
    parser.add_argument("query", nargs="?", default="", help="Search query, such as weather or skill search")
    parser.add_argument("--sort", choices=["installs", "stars", "downloads", "recent", ""], default="installs")
    parser.add_argument("--json", action="store_true", help="Return JSON")
    args = parser.parse_args()

    query = args.query.strip().lower()
    leads = [
        lead
        for lead in KNOWN_LEADS
        if not query or query in lead.name.lower() or query in lead.note.lower() or query in lead.source.lower()
    ]
    if query and ("skill" in query or "find" in query or "search" in query):
        leads = KNOWN_LEADS

    cli_output = run_clawhub(args.query, args.sort if args.sort != "downloads" else "installs")
    payload = {
        "query": args.query,
        "sort": args.sort,
        "known_leads": [asdict(lead) for lead in leads],
        "clawhub_cli_output": cli_output,
        "safety_note": "Review SKILL.md and scripts before copying into CowWechat; pending or unknown audits are not trusted by default.",
    }

    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 0

    print(f"Query: {args.query or '(known leads)'}")
    for lead in leads:
        bits = [lead.source, lead.url]
        if lead.downloads:
            bits.append(f"downloads {lead.downloads}")
        if lead.stars:
            bits.append(f"stars/votes {lead.stars}")
        if lead.audit:
            bits.append(f"audit {lead.audit}")
        print(f"- {lead.name}: " + " | ".join(bits))
        print(f"  {lead.note}")
    if cli_output:
        print("\nClawHub CLI output:")
        print(cli_output)
    else:
        print("\nClawHub CLI not available via clawhub/npx; use web lookup plus local review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
