"""Keep selected Clash Verge rules pinned to DIRECT after subscription updates."""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from collections import Counter
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Iterable, List, Optional, Sequence, Set, Tuple


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
EXTERNAL_CONTROLLER_RE = re.compile(r"^\s*external-controller\s*:\s*(?P<value>[^#]+?)\s*(?:#.*)?$")
SECRET_RE = re.compile(r"^\s*secret\s*:\s*(?P<value>[^#]*?)\s*(?:#.*)?$")


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


@dataclass(frozen=True)
class ReloadResult:
    attempted: bool
    changed: bool
    dry_run: bool
    config_path: Optional[Path]
    controller: str
    ok: bool
    message: str

    def to_dict(self) -> dict:
        return {
            "attempted": self.attempted,
            "changed": self.changed,
            "dry_run": self.dry_run,
            "config_path": str(self.config_path) if self.config_path else None,
            "controller": self.controller,
            "ok": self.ok,
            "message": self.message,
        }


@dataclass(frozen=True)
class GuardRunResult:
    patches: Sequence[PatchResult]
    reload: ReloadResult

    def to_dict(self) -> dict:
        return {
            "patches": [patch.to_dict() for patch in self.patches],
            "reload": self.reload.to_dict(),
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


def resolve_runtime_profile(root: Path) -> Optional[Path]:
    runtime_profile = root / "clash-verge.yaml"
    return runtime_profile if runtime_profile.is_file() else None


def run_guard(
    *,
    root: Optional[Path] = None,
    profile_path: Optional[Path] = None,
    runtime_profile_path: Optional[Path] = None,
    controller: Optional[str] = None,
    required_rules: Sequence[str] = DEFAULT_RULES,
    dry_run: bool = False,
    backup: bool = False,
    apply_runtime: bool = True,
    reload_core: bool = True,
    reload_always: bool = False,
) -> GuardRunResult:
    resolved_root = root or default_clash_verge_root()
    patch_targets = _resolve_patch_targets(
        resolved_root,
        profile_path=profile_path,
        runtime_profile_path=runtime_profile_path,
        apply_runtime=apply_runtime,
    )
    patches = [
        ensure_direct_rules(target, required_rules=required_rules, dry_run=dry_run, backup=backup)
        for target in patch_targets
    ]
    runtime_target = _select_runtime_target(resolved_root, profile_path, runtime_profile_path, patch_targets)
    runtime_changed = _path_was_changed(runtime_target, patches)
    if not reload_core:
        reload_result = ReloadResult(
            attempted=False,
            changed=runtime_changed or reload_always,
            dry_run=dry_run,
            config_path=runtime_target,
            controller=controller or "",
            ok=True,
            message="reload disabled",
        )
    elif not apply_runtime:
        reload_result = ReloadResult(
            attempted=False,
            changed=any(patch.changed for patch in patches),
            dry_run=dry_run,
            config_path=runtime_target,
            controller=controller or "",
            ok=True,
            message="runtime apply disabled",
        )
    else:
        reload_result = reload_runtime_config(
            runtime_target,
            controller=controller,
            dry_run=dry_run,
            changed=runtime_changed or reload_always,
        )
    return GuardRunResult(patches=patches, reload=reload_result)


def ensure_direct_rules(
    profile_path: Path,
    required_rules: Sequence[str] = DEFAULT_RULES,
    *,
    dry_run: bool = False,
    backup: bool = False,
) -> PatchResult:
    document = read_text_document(profile_path)
    patched_text, inserted_rules = _patch_rules_text(document, required_rules)
    changed = patched_text != document.text

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


def reload_runtime_config(
    config_path: Optional[Path],
    *,
    controller: Optional[str] = None,
    dry_run: bool = False,
    changed: bool = True,
    timeout_seconds: float = 5.0,
) -> ReloadResult:
    if not changed:
        return ReloadResult(
            attempted=False,
            changed=False,
            dry_run=dry_run,
            config_path=config_path,
            controller=controller or "",
            ok=True,
            message="no rule changes; reload skipped",
        )
    if not config_path or not config_path.is_file():
        return ReloadResult(
            attempted=False,
            changed=True,
            dry_run=dry_run,
            config_path=config_path,
            controller=controller or "",
            ok=False,
            message="runtime config not found; reload skipped",
        )

    document = read_text_document(config_path)
    controller_url = _normalize_controller_url(controller or _parse_external_controller(document.text))
    secret = _parse_secret(document.text)
    if dry_run:
        return ReloadResult(
            attempted=False,
            changed=True,
            dry_run=True,
            config_path=config_path,
            controller=controller_url,
            ok=True,
            message="dry-run; reload skipped",
        )
    if not controller_url:
        return ReloadResult(
            attempted=False,
            changed=True,
            dry_run=dry_run,
            config_path=config_path,
            controller="",
            ok=False,
            message="external-controller not found; reload skipped",
        )

    try:
        _put_reload_request(controller_url, config_path, secret=secret, timeout_seconds=timeout_seconds)
        return ReloadResult(
            attempted=True,
            changed=True,
            dry_run=False,
            config_path=config_path,
            controller=controller_url,
            ok=True,
            message="core reloaded",
        )
    except (OSError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        return ReloadResult(
            attempted=True,
            changed=True,
            dry_run=False,
            config_path=config_path,
            controller=controller_url,
            ok=False,
            message=f"reload failed: {exc}",
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
    apply_runtime: bool,
    reload_core: bool,
    reload_always: bool,
    controller: Optional[str],
    runtime_profile_path: Optional[Path],
) -> int:
    last_signature: Optional[tuple] = None
    while True:
        resolved_root = root or default_clash_verge_root()
        targets = _resolve_patch_targets(
            resolved_root,
            profile_path=profile_path,
            runtime_profile_path=runtime_profile_path,
            apply_runtime=apply_runtime,
        )
        signature = tuple(_file_signature(target) for target in targets)
        if signature != last_signature:
            result = run_guard(
                root=resolved_root,
                profile_path=profile_path,
                runtime_profile_path=runtime_profile_path,
                controller=controller,
                dry_run=dry_run,
                backup=backup,
                apply_runtime=apply_runtime,
                reload_core=reload_core,
                reload_always=reload_always,
            )
            _print_run_result(result, json_output=json_output)
            targets_after = _resolve_patch_targets(
                resolved_root,
                profile_path=profile_path,
                runtime_profile_path=runtime_profile_path,
                apply_runtime=apply_runtime,
            )
            last_signature = tuple(_file_signature(target) for target in targets_after)
        time.sleep(interval_seconds)


def _resolve_patch_targets(
    root: Path,
    *,
    profile_path: Optional[Path],
    runtime_profile_path: Optional[Path],
    apply_runtime: bool,
) -> List[Path]:
    targets = [profile_path or resolve_current_profile(root)]
    if apply_runtime and (profile_path is None or runtime_profile_path is not None):
        runtime_profile = runtime_profile_path or resolve_runtime_profile(root)
        if runtime_profile:
            targets.append(runtime_profile)
    return _dedupe_paths(targets)


def _dedupe_paths(paths: Iterable[Path]) -> List[Path]:
    seen: Set[Path] = set()
    result: List[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            result.append(path)
    return result


def _select_runtime_target(
    root: Path,
    profile_path: Optional[Path],
    runtime_profile_path: Optional[Path],
    targets: Sequence[Path],
) -> Optional[Path]:
    if runtime_profile_path:
        return runtime_profile_path
    runtime_profile = resolve_runtime_profile(root)
    if runtime_profile:
        return runtime_profile
    if profile_path:
        return profile_path
    return targets[-1] if targets else None


def _path_was_changed(target: Optional[Path], patches: Sequence[PatchResult]) -> bool:
    if not target:
        return False
    resolved_target = target.resolve()
    return any(patch.changed and patch.path.resolve() == resolved_target for patch in patches)


def _patch_rules_text(document: TextDocument, required_rules: Sequence[str]) -> Tuple[str, List[str]]:
    lines = document.text.splitlines()
    rules_index = _find_rules_index(lines)
    rules_body = lines[rules_index + 1 :]
    item_indent = _detect_rule_item_indent(rules_body)
    if item_indent is None:
        item_indent = f"{_rules_indent(lines[rules_index])}  "

    existing_rules = _collect_existing_rules(rules_body)
    missing_rules = [rule for rule in required_rules if rule not in existing_rules]
    normalized_lines, normalized = _normalize_required_rule_indents(lines, rules_index, required_rules, item_indent)
    if not missing_rules and not normalized:
        return document.text, []

    insert_lines = [f"{item_indent}- '{rule}'" for rule in missing_rules]
    patched_lines = normalized_lines[: rules_index + 1] + insert_lines + normalized_lines[rules_index + 1 :]
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


def _detect_rule_item_indent(lines_after_rules: Sequence[str]) -> Optional[str]:
    indents: List[str] = []
    for line in lines_after_rules:
        if _starts_new_top_level_section(line):
            break
        match = LIST_ITEM_RE.match(line)
        if match:
            indents.append(match.group("indent"))
    if not indents:
        return None
    counts = Counter(indents)
    return sorted(counts.items(), key=lambda item: (-item[1], len(item[0])))[0][0]


def _normalize_required_rule_indents(
    lines: Sequence[str],
    rules_index: int,
    required_rules: Sequence[str],
    item_indent: str,
) -> Tuple[List[str], bool]:
    required = set(required_rules)
    normalized_lines = list(lines)
    changed = False
    for index in range(rules_index + 1, len(lines)):
        line = lines[index]
        if _starts_new_top_level_section(line):
            break
        rule = _normalize_rule_line(line)
        if rule in required:
            normalized_line = f"{item_indent}- '{rule}'"
            if line != normalized_line:
                normalized_lines[index] = normalized_line
                changed = True
    return normalized_lines, changed


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


def _parse_external_controller(text: str) -> str:
    for line in text.splitlines():
        match = EXTERNAL_CONTROLLER_RE.match(line)
        if match:
            return _strip_yaml_scalar(match.group("value"))
    return ""


def _parse_secret(text: str) -> str:
    for line in text.splitlines():
        match = SECRET_RE.match(line)
        if match:
            return _strip_yaml_scalar(match.group("value"))
    return ""


def _strip_yaml_scalar(value: str) -> str:
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
        return value[1:-1]
    return value


def _normalize_controller_url(controller: str) -> str:
    controller = controller.strip().rstrip("/")
    if not controller:
        return ""
    if "://" not in controller:
        controller = f"http://{controller}"
    return controller


def _put_reload_request(controller_url: str, config_path: Path, *, secret: str, timeout_seconds: float) -> None:
    body = json.dumps({"path": str(config_path)}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{controller_url}/configs?force=true",
        data=body,
        method="PUT",
        headers={"Content-Type": "application/json"},
    )
    if secret:
        request.add_header("Authorization", f"Bearer {secret}")
    with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
        response.read()


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


def _print_run_result(result: GuardRunResult, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(result.to_dict(), ensure_ascii=False))
        return

    for patch in result.patches:
        _print_result(patch, json_output=False)
    reload_result = result.reload
    if reload_result.attempted:
        status = "succeeded" if reload_result.ok else "failed"
        print(f"runtime reload {status}: {reload_result.message}")
    elif reload_result.changed or reload_result.dry_run:
        print(f"runtime reload skipped: {reload_result.message}")


def _parse_args(argv: Optional[Sequence[str]]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Ensure Xiaohongshu and Douyin use DIRECT rules in the active Clash Verge profile."
    )
    parser.add_argument("--root", type=Path, help="Clash Verge config root. Defaults to APPDATA Clash Verge Rev.")
    parser.add_argument("--profile", type=Path, help="Patch this Clash profile file instead of resolving the active one.")
    parser.add_argument("--runtime-profile", type=Path, help="Patch and reload this generated runtime config.")
    parser.add_argument("--controller", help="Override external-controller URL, for example http://127.0.0.1:9097.")
    parser.add_argument(
        "--no-runtime-apply",
        action="store_true",
        help="Only patch the subscription profile; do not patch the generated runtime config.",
    )
    parser.add_argument("--no-reload", action="store_true", help="Do not call the Clash/Mihomo reload API.")
    parser.add_argument("--reload-always", action="store_true", help="Reload the runtime config even when rules were already present.")
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
                apply_runtime=not args.no_runtime_apply,
                reload_core=not args.no_reload,
                reload_always=args.reload_always,
                controller=args.controller,
                runtime_profile_path=args.runtime_profile,
            )

        result = run_guard(
            root=args.root,
            profile_path=args.profile,
            runtime_profile_path=args.runtime_profile,
            controller=args.controller,
            dry_run=args.dry_run,
            backup=args.backup,
            apply_runtime=not args.no_runtime_apply,
            reload_core=not args.no_reload,
            reload_always=args.reload_always,
        )
        _print_run_result(result, json_output=args.json)
        return 0 if result.reload.ok else 1
    except KeyboardInterrupt:
        return 130
    except RuleGuardError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
