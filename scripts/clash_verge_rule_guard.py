"""Keep selected Clash Verge rules pinned to DIRECT after subscription updates."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set


DEFAULT_RULES = [
    "DOMAIN-SUFFIX,xiaohongshu.com,DIRECT",
    "DOMAIN-SUFFIX,xiaohongshu.net,DIRECT",
    "DOMAIN-SUFFIX,xhscdn.com,DIRECT",
    "DOMAIN-SUFFIX,xhslink.com,DIRECT",
    "DOMAIN-KEYWORD,xiaohongshu,DIRECT",
    "DOMAIN-KEYWORD,xhscdn,DIRECT",
    "DOMAIN-SUFFIX,douyin.com,DIRECT",
    "DOMAIN-SUFFIX,douyinstatic.com,DIRECT",
    "DOMAIN-SUFFIX,douyincdn.com,DIRECT",
    "DOMAIN-SUFFIX,douyinpic.com,DIRECT",
    "DOMAIN-SUFFIX,iesdouyin.com,DIRECT",
    "DOMAIN-SUFFIX,amemv.com,DIRECT",
    "DOMAIN-SUFFIX,snssdk.com,DIRECT",
    "DOMAIN-SUFFIX,pstatp.com,DIRECT",
    "DOMAIN-SUFFIX,byteimg.com,DIRECT",
    "DOMAIN-SUFFIX,bytecdn.cn,DIRECT",
    "DOMAIN-SUFFIX,bytedance.com,DIRECT",
    "DOMAIN-SUFFIX,bytedanceapi.com,DIRECT",
    "DOMAIN-KEYWORD,douyin,DIRECT",
]

CLASH_VERGE_REV_DIR = "io.github.clash-verge-rev.clash-verge-rev"
RULES_HEADER_RE = re.compile(r"^(?P<indent>\s*)rules\s*:\s*(?:#.*)?$")
LIST_ITEM_RE = re.compile(r"^(?P<indent>\s*)-\s+(?P<body>.+?)\s*(?:#.*)?$")
CURRENT_PROFILE_RE = re.compile(r"^\s*current\s*:\s*(?P<uid>[^#\s]+)\s*(?:#.*)?$")


class RuleGuardError(RuntimeError):
    """Raised when the Clash profile cannot be safely patched."""


@dataclass(frozen=True)
class TextDocument:
    text: str
    encoding: str
    newline: str
    trailing_newline: bool


@dataclass(frozen=True)
class PatchResult:
    path: Path
    changed: bool
    dry_run: bool
    inserted_rules: Sequence[str]

    def to_dict(self) -> dict:
        return {
            "path": str(self.path),
            "changed": self.changed,
            "dry_run": self.dry_run,
            "inserted_rules": list(self.inserted_rules),
        }


def default_clash_verge_root() -> Path:
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise RuleGuardError("APPDATA is not set; pass --root or --profile explicitly.")
    return Path(appdata) / CLASH_VERGE_REV_DIR


def resolve_current_profile(root: Path) -> Path:
    profiles_config = root / "profiles.yaml"
    if not profiles_config.is_file():
        fallback = root / "clash-verge.yaml"
        if fallback.is_file():
            return fallback
        raise RuleGuardError(f"Cannot find profiles.yaml under {root}")

    document = read_text_document(profiles_config)
    current_uid = _parse_current_uid(document.text)
    if not current_uid:
        raise RuleGuardError(f"Cannot find current profile uid in {profiles_config}")

    profile_path = root / "profiles" / f"{current_uid}.yaml"
    if profile_path.is_file():
        return profile_path

    fallback = root / "clash-verge.yaml"
    if fallback.is_file():
        return fallback
    raise RuleGuardError(f"Cannot find current profile file for uid {current_uid}")


def ensure_direct_rules(
    profile_path: Path,
    required_rules: Sequence[str] = DEFAULT_RULES,
    *,
    dry_run: bool = False,
    backup: bool = False,
) -> PatchResult:
    document = read_text_document(profile_path)
    patched_text, inserted_rules = _patch_rules_text(document, required_rules)
    changed = bool(inserted_rules)

    if changed and not dry_run:
        if backup:
            _write_backup(profile_path)
        with profile_path.open("w", encoding=document.encoding, newline="") as stream:
            stream.write(patched_text)

    return PatchResult(
        path=profile_path,
        changed=changed,
        dry_run=dry_run,
        inserted_rules=inserted_rules,
    )


def read_text_document(path: Path) -> TextDocument:
    data = path.read_bytes()
    encodings = ("utf-8-sig", "utf-8", "gb18030") if data.startswith(b"\xef\xbb\xbf") else ("utf-8", "gb18030")
    for encoding in encodings:
        try:
            text = data.decode(encoding)
            return TextDocument(
                text=text,
                encoding=encoding,
                newline=_detect_newline(text),
                trailing_newline=text.endswith(("\n", "\r\n")),
            )
        except UnicodeDecodeError:
            continue
    raise RuleGuardError(f"Cannot decode text file: {path}")


def watch_profile(
    root: Optional[Path],
    profile_path: Optional[Path],
    *,
    interval_seconds: float,
    dry_run: bool,
    backup: bool,
    json_output: bool,
) -> int:
    last_signature: Optional[tuple] = None
    while True:
        target = profile_path or resolve_current_profile(root or default_clash_verge_root())
        signature = _file_signature(target)
        if signature != last_signature:
            result = ensure_direct_rules(target, dry_run=dry_run, backup=backup)
            _print_result(result, json_output=json_output)
            last_signature = _file_signature(target)
        time.sleep(interval_seconds)


def _patch_rules_text(document: TextDocument, required_rules: Sequence[str]) -> tuple[str, List[str]]:
    lines = document.text.splitlines()
    rules_index = _find_rules_index(lines)
    existing_rules = _collect_existing_rules(lines[rules_index + 1 :])
    missing_rules = [rule for rule in required_rules if rule not in existing_rules]
    if not missing_rules:
        return document.text, []

    item_indent = _detect_rule_item_indent(lines[rules_index + 1 :]) or f"{_rules_indent(lines[rules_index])}  "
    insert_lines = [f"{item_indent}- '{rule}'" for rule in missing_rules]
    patched_lines = lines[: rules_index + 1] + insert_lines + lines[rules_index + 1 :]
    patched_text = document.newline.join(patched_lines)
    if document.trailing_newline:
        patched_text += document.newline
    return patched_text, missing_rules


def _find_rules_index(lines: Sequence[str]) -> int:
    for index, line in enumerate(lines):
        if RULES_HEADER_RE.match(line):
            return index
    raise RuleGuardError("Cannot find a top-level rules: section in the profile.")


def _collect_existing_rules(lines_after_rules: Sequence[str]) -> Set[str]:
    rules: Set[str] = set()
    for line in lines_after_rules:
        if _starts_new_top_level_section(line):
            break
        normalized = _normalize_rule_line(line)
        if normalized:
            rules.add(normalized)
    return rules


def _normalize_rule_line(line: str) -> str:
    match = LIST_ITEM_RE.match(line)
    if not match:
        return ""
    body = match.group("body").strip()
    if len(body) >= 2 and body[0] == body[-1] and body[0] in ("'", '"'):
        body = body[1:-1]
    return body.strip()


def _detect_rule_item_indent(lines_after_rules: Sequence[str]) -> str:
    for line in lines_after_rules:
        if _starts_new_top_level_section(line):
            return ""
        match = LIST_ITEM_RE.match(line)
        if match:
            return match.group("indent")
    return ""


def _starts_new_top_level_section(line: str) -> bool:
    return bool(line and not line.startswith((" ", "\t", "-")) and ":" in line)


def _rules_indent(line: str) -> str:
    match = RULES_HEADER_RE.match(line)
    return match.group("indent") if match else ""


def _parse_current_uid(text: str) -> str:
    for line in text.splitlines():
        match = CURRENT_PROFILE_RE.match(line)
        if match:
            return match.group("uid").strip("'\"")
    return ""


def _detect_newline(text: str) -> str:
    return "\r\n" if "\r\n" in text else "\n"


def _write_backup(path: Path) -> None:
    timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup_path = path.with_name(f"{path.name}.{timestamp}.bak")
    backup_path.write_bytes(path.read_bytes())


def _file_signature(path: Path) -> tuple:
    stat = path.stat()
    return (path, stat.st_mtime_ns, stat.st_size)


def _print_result(result: PatchResult, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
        return

    if result.changed:
        mode = "would insert" if result.dry_run else "inserted"
        print(f"{mode} {len(result.inserted_rules)} rule(s) into {result.path}")
        for rule in result.inserted_rules:
            print(f"  - {rule}")
    else:
        print(f"all required DIRECT rules already exist in {result.path}")


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ensure Xiaohongshu and Douyin use DIRECT rules in the active Clash Verge profile."
    )
    parser.add_argument("--root", type=Path, help="Clash Verge config root. Defaults to APPDATA Clash Verge Rev.")
    parser.add_argument("--profile", type=Path, help="Patch this Clash profile file instead of resolving the active one.")
    parser.add_argument("--watch", action="store_true", help="Keep running and patch again after profile file changes.")
    parser.add_argument("--interval", type=float, default=10.0, help="Polling interval for --watch, in seconds.")
    parser.add_argument("--dry-run", action="store_true", help="Report missing rules without writing the profile.")
    parser.add_argument("--backup", action="store_true", help="Write a timestamped .bak before modifying the profile.")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON results.")
    return parser.parse_args(argv)


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _parse_args(argv)
    try:
        if args.watch:
            return watch_profile(
                args.root,
                args.profile,
                interval_seconds=args.interval,
                dry_run=args.dry_run,
                backup=args.backup,
                json_output=args.json,
            )

        target = args.profile or resolve_current_profile(args.root or default_clash_verge_root())
        result = ensure_direct_rules(target, dry_run=args.dry_run, backup=args.backup)
        _print_result(result, json_output=args.json)
        return 0
    except KeyboardInterrupt:
        return 130
    except RuleGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
