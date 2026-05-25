# encoding:utf-8
"""Local skill catalog cache for fast user-facing skill lookups."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Tuple

from agent.skills.loader import SkillLoader
from agent.skills.types import SkillEntry


_CATALOG_CACHE_LOCK = threading.Lock()
_CATALOG_CACHES: Dict[Tuple[str, str], "SkillCatalogCache"] = {}


@dataclass(frozen=True)
class SkillCatalogEntry:
    """Compact metadata needed for local skill list and usage replies."""

    name: str
    display_name: str
    description: str
    source: str
    enabled: bool
    file_path: str
    base_dir: str
    content: str

    def as_dict(self) -> dict:
        return {
            "name": self.name,
            "display_name": self.display_name,
            "description": self.description,
            "source": self.source,
            "enabled": self.enabled,
            "file_path": self.file_path,
            "base_dir": self.base_dir,
        }


@dataclass(frozen=True)
class SkillCatalogSnapshot:
    entries: Tuple[SkillCatalogEntry, ...]
    fingerprint: Tuple[Tuple[str, int, int], ...]

    @property
    def by_name(self) -> Dict[str, SkillCatalogEntry]:
        return {entry.name: entry for entry in self.entries}


class SkillCatalogCache:
    """Read-through catalog cache refreshed when local skill files change."""

    def __init__(
        self,
        builtin_dir: str,
        custom_dir: str,
        loader: Optional[SkillLoader] = None,
    ) -> None:
        self.builtin_dir = os.path.abspath(os.path.expanduser(builtin_dir))
        self.custom_dir = os.path.abspath(os.path.expanduser(custom_dir))
        self.loader = loader or SkillLoader()
        self._lock = threading.Lock()
        self._snapshot: Optional[SkillCatalogSnapshot] = None

    def invalidate(self) -> None:
        with self._lock:
            self._snapshot = None

    def refresh_from_entries(
        self,
        skills: Mapping[str, SkillEntry],
        skills_config: Mapping[str, Mapping[str, object]],
    ) -> SkillCatalogSnapshot:
        snapshot = SkillCatalogSnapshot(
            tuple(self._entries_from_loaded(skills, skills_config)),
            self._fingerprint(),
        )
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def snapshot(self, *, force: bool = False) -> SkillCatalogSnapshot:
        fingerprint = self._fingerprint()
        with self._lock:
            cached = self._snapshot
            if not force and cached is not None and cached.fingerprint == fingerprint:
                return cached

        skills = self.loader.load_all_skills(
            builtin_dir=self.builtin_dir,
            custom_dir=self.custom_dir,
        )
        config = self._load_skills_config()
        snapshot = SkillCatalogSnapshot(
            tuple(self._entries_from_loaded(skills, config)),
            fingerprint,
        )
        with self._lock:
            self._snapshot = snapshot
        return snapshot

    def list_entries(self) -> List[SkillCatalogEntry]:
        return list(self.snapshot().entries)

    def find_entry(self, name: str) -> Optional[SkillCatalogEntry]:
        query = _normalize_lookup(name)
        if not query:
            return None

        entries = self.snapshot().entries
        for entry in entries:
            if query in {_normalize_lookup(entry.name), _normalize_lookup(entry.display_name)}:
                return entry

        partial = [
            entry
            for entry in entries
            if query in _normalize_lookup(entry.name)
            or query in _normalize_lookup(entry.display_name)
        ]
        return partial[0] if len(partial) == 1 else None

    def find_entry_in_text(self, text: str) -> Optional[SkillCatalogEntry]:
        normalized_text = _normalize_lookup(text)
        if not normalized_text:
            return None

        matches: List[SkillCatalogEntry] = []
        for entry in self.snapshot().entries:
            aliases = {
                _normalize_lookup(entry.name),
                _normalize_lookup(entry.display_name),
            }
            if any(alias and alias in normalized_text for alias in aliases):
                matches.append(entry)
        if not matches:
            return None
        matches.sort(key=lambda entry: len(entry.name), reverse=True)
        return matches[0]

    def format_local_list(self) -> str:
        entries = sorted(self.snapshot().entries, key=lambda entry: entry.name)
        if not entries:
            return "暂无已安装的技能\n\n提示：/skill list --remote 可浏览技能广场"

        enabled_count = sum(1 for entry in entries if entry.enabled)
        lines = [f"本地技能/功能 ({enabled_count}/{len(entries)})", ""]
        for entry in entries:
            icon = "[on]" if entry.enabled else "[off]"
            line = f"{icon} {entry.display_name or entry.name}"
            if entry.display_name and entry.display_name != entry.name:
                line += f" ({entry.name})"
            if entry.description:
                line += f"\n   {_shorten(entry.description, 80)}"
            if entry.source:
                line += f"\n   来源: {entry.source}"
            lines.extend([line, ""])

        lines.append("提示：/skill list --remote 浏览技能广场")
        lines.append("提示：/skill usage <名称> 查看用法")
        return "\n".join(lines)

    def format_skill_usage(self, name: str) -> str:
        entry = self.find_entry(name)
        if entry is None:
            return f"技能 '{name}' 未找到"

        label = entry.display_name or entry.name
        lines = [f"技能用法: {label}", ""]
        if label != entry.name:
            lines.append(f"  名称: {entry.name}")
        if entry.description:
            lines.append(f"  描述: {entry.description}")
        lines.append(f"  来源: {entry.source or 'local'}")
        lines.append(f"  状态: {'启用' if entry.enabled else '禁用'}")

        usage = _extract_usage_preview(entry.content)
        if usage:
            lines.extend(["", usage])
        lines.append("")
        lines.append(f"提示：可发送 `/skill usage {entry.name}` 再次查看，或直接描述任务让 Agent 按需使用。")
        return "\n".join(lines)

    def _load_skills_config(self) -> Dict[str, dict]:
        path = os.path.join(self.custom_dir, "skills_config.json")
        if not os.path.exists(path):
            return {}
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _entries_from_loaded(
        self,
        skills: Mapping[str, SkillEntry],
        skills_config: Mapping[str, Mapping[str, object]],
    ) -> List[SkillCatalogEntry]:
        entries: List[SkillCatalogEntry] = []
        for name, loaded in skills.items():
            skill = loaded.skill
            prev = skills_config.get(name, {})
            enabled = bool(prev.get("enabled", True))
            display_name = _resolve_display_name(skill.frontmatter, prev.get("display_name")) or name
            entries.append(
                SkillCatalogEntry(
                    name=name,
                    display_name=display_name,
                    description=skill.description,
                    source=str(prev.get("source") or skill.source or ""),
                    enabled=enabled,
                    file_path=skill.file_path,
                    base_dir=skill.base_dir,
                    content=skill.content,
                )
            )
        return entries

    def _fingerprint(self) -> Tuple[Tuple[str, int, int], ...]:
        records: List[Tuple[str, int, int]] = []
        for root in (self.builtin_dir, self.custom_dir):
            records.extend(_skill_file_records(root))
        config_path = os.path.join(self.custom_dir, "skills_config.json")
        if os.path.exists(config_path):
            records.append(_path_record(config_path))
        return tuple(sorted(records))


def get_skill_catalog_cache(builtin_dir: str, custom_dir: str) -> SkillCatalogCache:
    key = (
        os.path.abspath(os.path.expanduser(builtin_dir)),
        os.path.abspath(os.path.expanduser(custom_dir)),
    )
    with _CATALOG_CACHE_LOCK:
        cache = _CATALOG_CACHES.get(key)
        if cache is None:
            cache = SkillCatalogCache(*key)
            _CATALOG_CACHES[key] = cache
        return cache


def invalidate_skill_catalog_cache(
    builtin_dir: Optional[str] = None,
    custom_dir: Optional[str] = None,
) -> None:
    with _CATALOG_CACHE_LOCK:
        if builtin_dir is None or custom_dir is None:
            for cache in _CATALOG_CACHES.values():
                cache.invalidate()
            return
        key = (
            os.path.abspath(os.path.expanduser(builtin_dir)),
            os.path.abspath(os.path.expanduser(custom_dir)),
        )
        cache = _CATALOG_CACHES.get(key)
        if cache is not None:
            cache.invalidate()


def _skill_file_records(root: str) -> Iterable[Tuple[str, int, int]]:
    path = Path(root)
    if not path.exists() or not path.is_dir():
        return []

    records: List[Tuple[str, int, int]] = []
    for child in path.iterdir():
        if child.name.startswith("."):
            continue
        if child.is_file() and child.suffix.lower() == ".md" and child.name.upper() != "README.MD":
            records.append(_path_record(str(child)))

    for skill_file in path.rglob("SKILL.md"):
        if any(part.startswith(".") for part in skill_file.parts):
            continue
        records.append(_path_record(str(skill_file)))
    return records


def _path_record(path: str) -> Tuple[str, int, int]:
    try:
        stat = os.stat(path)
        return (os.path.abspath(path), int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        return (os.path.abspath(path), 0, 0)


def _normalize_lookup(text: str) -> str:
    return re.sub(r"[\s_\-./\\:，,。！？?（）()\[\]【】<>《》]+", "", str(text or "").lower())


def _resolve_display_name(frontmatter: Mapping[str, object], previous: object) -> str:
    previous_name = _normalize_display_name(previous)
    frontmatter_name = _frontmatter_display_name(frontmatter)
    if frontmatter_name and (not previous_name or _looks_like_placeholder_mojibake(previous_name)):
        return frontmatter_name
    return previous_name or frontmatter_name


def _frontmatter_display_name(frontmatter: Mapping[str, object]) -> str:
    for key in ("display-name", "display_name"):
        display_name = _normalize_display_name(frontmatter.get(key))
        if display_name:
            return display_name
    return ""


def _normalize_display_name(value: object) -> str:
    if isinstance(value, list):
        value = value[0] if value else ""
    if value is None:
        return ""
    return str(value).strip()


def _looks_like_placeholder_mojibake(value: str) -> bool:
    stripped = value.strip()
    return stripped.count("?") >= 2


def _shorten(text: str, limit: int) -> str:
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def _strip_frontmatter(content: str) -> str:
    if not content.startswith("---"):
        return content
    end = content.find("\n---", 3)
    if end == -1:
        return content
    return content[end + 4 :].lstrip("\n")


def _extract_usage_preview(content: str) -> str:
    body = _strip_frontmatter(content)
    if not body.strip():
        return ""

    section = _find_usage_section(body)
    if section:
        return section

    lines = [line.rstrip() for line in body.splitlines()]
    preview = [line for line in lines if line.strip()][:24]
    return "\n".join(preview)


def _find_usage_section(body: str) -> str:
    headings = (
        "usage",
        "how to use",
        "commands",
        "command",
        "script",
        "用法",
        "使用",
        "命令",
    )
    lines = body.splitlines()
    start = None
    for index, line in enumerate(lines):
        match = re.match(r"^(#{1,3})\s+(.+?)\s*$", line)
        if not match:
            continue
        level = len(match.group(1))
        title = match.group(2).strip().lower()
        if level == 1 and title not in headings:
            continue
        if any(marker in title for marker in headings):
            start = index
            break
    if start is None:
        return ""

    collected: List[str] = []
    for line in lines[start : start + 32]:
        if collected and re.match(r"^#{1,3}\s+", line):
            break
        collected.append(line.rstrip())
    return "\n".join(collected).strip()
