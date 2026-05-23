#!/usr/bin/env python3
"""Small ClawHub/OpenClaw skill discovery helper for CowWechat."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass


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


def run_clawhub(query: str, sort: str) -> str | None:
    npx = shutil.which("npx")
    if not npx:
        return None
    cmd = [npx, "clawhub", "search", query]
    # Current clawhub CLI exposes vector search plus --limit. Ranking flags vary
    # between releases, so popularity sorting is handled by listing metadata.
    if sort:
        cmd.extend(["--limit", "10"])
    try:
        completed = subprocess.run(
            cmd,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
        )
    except Exception as exc:
        return f"clawhub_cli_error: {exc}"
    if completed.returncode != 0:
        return f"clawhub_cli_error: {completed.stderr.strip() or completed.stdout.strip()}"
    return completed.stdout.strip()


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
        print("\nClawHub CLI not available via npx; use web lookup plus local review.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
