# encoding:utf-8
"""Local skill catalog cache for fast user-facing skill lookups."""

from __future__ import annotations

import json
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from agent.skills.loader import SkillLoader
from agent.skills.types import SkillEntry


_CATALOG_CACHE_LOCK = threading.Lock()
_CATALOG_CACHES: Dict[Tuple[str, str], "SkillCatalogCache"] = {}
FULL_SKILL_FALLBACK_PREFIX = "[[READ_FULL_SKILL"

_CATEGORY_SPECS: Dict[str, Dict[str, object]] = {
    "travel_location": {
        "label": "出行地图",
        "keywords": ("高德", "地图", "路线", "路况", "通勤", "旅行", "旅游", "出行", "交通", "天气", "12306", "火车", "票", "travel", "weather", "amap"),
    },
    "media_content": {
        "label": "内容媒体",
        "keywords": ("图片", "视频", "抖音", "哔哩", "bilibili", "meme", "表情包", "公众号", "文章", "image", "video"),
    },
    "documents": {
        "label": "文档文件",
        "keywords": ("pdf", "word", "docx", "ppt", "pptx", "xlsx", "excel", "markdown", "文件", "表格", "文档", "网盘"),
    },
    "web_research": {
        "label": "网页检索与浏览",
        "keywords": ("搜索", "网页", "浏览器", "browser", "playwright", "github", "可靠搜索", "资料", "最新", "新闻"),
    },
    "finance_market": {
        "label": "金融行情",
        "keywords": ("股票", "基金", "投资", "定投", "行情", "价格", "比特币", "黄金", "crypto", "stock", "market"),
    },
    "shopping_food": {
        "label": "购物餐饮",
        "keywords": ("外卖", "吃什么", "美团", "淘宝", "京东", "拼多多", "比价", "购物", "买东西", "网购", "优惠券", "商品", "餐饮", "点餐", "吃饭", "takeout", "shopping"),
    },
    "productivity": {
        "label": "办公协作",
        "keywords": ("企业微信", "wecom", "会议", "日程", "待办", "通讯录", "消息", "知识库", "knowledge", "wiki"),
    },
    "system_dev": {
        "label": "系统开发运维",
        "keywords": ("代码", "git", "github", "上传", "部署", "skill开发", "skill安装", "skill同步", "安全", "guard", "quota", "token", "额度", "自进化", "backend", "浏览器控制"),
    },
    "other": {
        "label": "其他",
        "keywords": (),
    },
}


@dataclass(frozen=True)
class SkillCatalogEntry:
    """Compact metadata needed for local skill list and usage replies."""

    name: str
    display_name: str
    description: str
    category: str
    category_label: str
    compact_summary: str
    detailed_summary: str
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
            "category": self.category,
            "category_label": self.category_label,
            "compact_summary": self.compact_summary,
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

    def overview_summary(self, max_chars: int = 12000) -> str:
        entries = sorted(
            self.snapshot().entries,
            key=lambda entry: (entry.category_label, not entry.enabled, entry.name),
        )
        if not entries:
            return "暂无已安装的技能/功能。"

        grouped: Dict[str, List[SkillCatalogEntry]] = {}
        for entry in entries:
            grouped.setdefault(entry.category_label or "其他", []).append(entry)

        lines = ["本机技能/功能总览（压缩摘要）："]
        for category_label in sorted(grouped):
            lines.append(f"\n## {category_label}")
            for entry in grouped[category_label]:
                status = "启用" if entry.enabled else "禁用"
                label = entry.display_name or entry.name
                summary = entry.compact_summary or entry.description
                lines.append(f"- {label} ({entry.name}) [{status}]：{_shorten(_one_line(summary), 150)}")
                if _text_size(lines) >= max_chars:
                    lines.append("...（其余技能已省略；请让用户缩小分类或指定 Skill 名称。）")
                    return "\n".join(lines)[:max_chars]
        return "\n".join(lines)[:max_chars]

    def category_summary_for_text(self, text: str, max_chars: int = 9000) -> Optional[str]:
        categories = self.find_categories_in_text(text)
        if not categories:
            return None
        return self.multi_category_summary(categories, max_chars=max_chars)

    def category_summary(self, category: str, max_chars: int = 9000) -> str:
        normalized = _normalize_category(category)
        entries = [
            entry for entry in self.snapshot().entries
            if entry.category == normalized or entry.category_label == category
        ]
        if not entries:
            if normalized in _CATEGORY_SPECS:
                label = str(_CATEGORY_SPECS[normalized]["label"])
                return f"{label}类技能摘要：\n暂无匹配这个分类的本地 Skill。"
            return ""

        entries.sort(key=lambda entry: (not entry.enabled, entry.name))
        label = entries[0].category_label or category
        lines = [f"{label}类技能摘要："]
        for entry in entries:
            status = "启用" if entry.enabled else "禁用"
            lines.append(f"\n## {entry.display_name or entry.name} ({entry.name}) [{status}]")
            lines.append(entry.detailed_summary or entry.compact_summary or entry.description)
            if _text_size(lines) >= max_chars:
                lines.append("...（本分类其余内容已省略；请指定具体 Skill 查看详细用法。）")
                break
        return "\n".join(lines)[:max_chars]

    def multi_category_summary(self, categories: Sequence[str] | str, max_chars: int = 12000) -> str:
        normalized_categories = self._normalize_category_list(categories)
        if not normalized_categories:
            return self.overview_summary(max_chars=max_chars)

        chunks: List[str] = []
        remaining = max_chars
        per_category_limit = max(2500, max_chars // max(1, len(normalized_categories)))
        for category in normalized_categories:
            summary = self.category_summary(category, max_chars=min(per_category_limit, remaining))
            if not summary:
                continue
            chunks.append(summary)
            remaining = max_chars - _text_size(chunks)
            if remaining <= 200:
                chunks.append("...（其余分类内容已省略；请让用户缩小分类或指定 Skill 名称。）")
                break
        return "\n\n".join(chunks)[:max_chars]

    def category_options_summary(self) -> str:
        counts: Dict[str, int] = {category: 0 for category in _CATEGORY_SPECS}
        for entry in self.snapshot().entries:
            counts[entry.category] = counts.get(entry.category, 0) + 1

        lines = ["可选 Skill 分类："]
        for category, spec in _CATEGORY_SPECS.items():
            if category == "other":
                continue
            keywords = "、".join(str(keyword) for keyword in tuple(spec["keywords"])[:12])
            lines.append(
                f"- {category}: {spec['label']}；已安装 {counts.get(category, 0)} 个；"
                f"常见说法示例：{keywords}"
            )
        lines.append("- other: 其他；只在无法归入上面任何分类时使用。")
        return "\n".join(lines)

    def find_category_in_text(self, text: str) -> str:
        categories = self.find_categories_in_text(text)
        return categories[0] if categories else ""

    def find_categories_in_text(self, text: str) -> List[str]:
        compact = _normalize_lookup(text)
        if not compact:
            return []
        matches: List[str] = []
        for category, spec in _CATEGORY_SPECS.items():
            if category == "other":
                continue
            if any(_normalize_lookup(keyword) in compact for keyword in spec["keywords"]):
                matches.append(category)
        return matches

    @staticmethod
    def _normalize_category_list(categories: Sequence[str] | str) -> List[str]:
        if isinstance(categories, str):
            raw_values = re.split(r"[,，|、\s]+", categories)
        else:
            raw_values = [str(category or "") for category in categories]

        normalized: List[str] = []
        for value in raw_values:
            category = _normalize_category(value.strip())
            if category and category not in normalized:
                normalized.append(category)
        return normalized

    def format_skill_detail_summary(self, name: str, max_chars: int = 9000) -> str:
        entry = self.find_entry(name)
        if entry is None:
            return f"技能 '{name}' 未找到"
        label = entry.display_name or entry.name
        lines = [f"单个 Skill 详细摘要：{label} ({entry.name})", ""]
        lines.append(entry.detailed_summary or entry.compact_summary or entry.description)
        lines.append("")
        lines.append("如果以上摘要不足以回答用户对内部流程、边界条件、脚本参数、配置或安全规则的追问，请返回 READ_FULL_SKILL 标记。")
        return "\n".join(lines)[:max_chars]

    def full_skill_context(self, name: str, max_chars: int = 40000) -> str:
        entry = self.find_entry(name)
        if entry is None:
            return f"技能 '{name}' 未找到"
        label = entry.display_name or entry.name
        content = _strip_frontmatter(entry.content).strip() or entry.content.strip()
        return (
            f"完整 SKILL.md：{label} ({entry.name})\n"
            f"文件：{entry.file_path}\n\n"
            f"{content}"
        )[:max_chars]

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
            category = _infer_category(name, display_name, skill.description, skill.content, prev, skill.frontmatter)
            compact_summary = _build_compact_summary(name, display_name, skill.description, skill.content)
            detailed_summary = _build_detailed_summary(name, display_name, skill.description, skill.content)
            entries.append(
                SkillCatalogEntry(
                    name=name,
                    display_name=display_name,
                    description=skill.description,
                    category=category,
                    category_label=_CATEGORY_SPECS.get(category, _CATEGORY_SPECS["other"])["label"],
                    compact_summary=compact_summary,
                    detailed_summary=detailed_summary,
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


def _text_size(lines: Iterable[str]) -> int:
    return sum(len(str(line)) + 1 for line in lines)


def _one_line(text: str) -> str:
    return re.sub(r"\s+", " ", str(text or "")).strip()


def _normalize_category(value: object) -> str:
    raw = str(value or "").strip().lower().replace("-", "_")
    if raw in _CATEGORY_SPECS:
        return raw
    compact = _normalize_lookup(raw)
    for key, spec in _CATEGORY_SPECS.items():
        if compact == _normalize_lookup(str(spec["label"])):
            return key
    return "other"


def _infer_category(
    name: str,
    display_name: str,
    description: str,
    content: str,
    config: Mapping[str, object],
    frontmatter: Mapping[str, object],
) -> str:
    configured = config.get("category") or frontmatter.get("category")
    if configured:
        normalized = _normalize_category(configured)
        if normalized != "other" or str(configured).strip().lower() in ("other", "其他"):
            return normalized

    text = _normalize_lookup(" ".join([name, display_name, description]))
    best_category = "other"
    best_score = 0
    for category, spec in _CATEGORY_SPECS.items():
        if category == "other":
            continue
        score = sum(1 for keyword in spec["keywords"] if _normalize_lookup(str(keyword)) in text)
        if score > best_score:
            best_category = category
            best_score = score
    if best_score:
        return best_category

    text = _normalize_lookup(_strip_frontmatter(content)[:1200])
    for category, spec in _CATEGORY_SPECS.items():
        if category == "other":
            continue
        score = sum(1 for keyword in spec["keywords"] if _normalize_lookup(str(keyword)) in text)
        if score > best_score:
            best_category = category
            best_score = score
    return best_category


def _build_compact_summary(name: str, display_name: str, description: str, content: str) -> str:
    parts = [_one_line(description)]
    triggers = _extract_trigger_markers(description)
    if triggers:
        parts.append("触发词：" + "、".join(triggers[:8]))
    commands = _extract_command_lines(content, limit=4)
    if commands:
        parts.append("示例：" + "；".join(commands))
    return " ".join(part for part in parts if part).strip() or (display_name or name)


def _build_detailed_summary(name: str, display_name: str, description: str, content: str) -> str:
    body = _strip_frontmatter(content)
    lines = []
    if description:
        lines.append("用途：" + _one_line(description))

    triggers = _extract_trigger_markers(description)
    if triggers:
        lines.append("触发词：" + "、".join(triggers[:16]))

    sections = _extract_relevant_sections(body, max_chars=6500)
    if sections:
        lines.append(sections)

    commands = _extract_command_lines(content, limit=20)
    if commands:
        lines.append("可直接使用的命令/脚本示例：")
        lines.extend(f"- {command}" for command in commands)

    if not lines:
        return display_name or name
    return "\n".join(lines).strip()


def _extract_trigger_markers(description: str) -> List[str]:
    text = str(description or "")
    match = re.search(r"(?:Use when|Triggers include|触发|适用).*", text, re.I)
    source = match.group(0) if match else text
    tokens = [
        token.strip(" `\"'“”‘’。，、；;:：()（）[]【】")
        for token in re.split(r"[,，、;/]| or | and | when | asks? |用户|提及|涉及", source, flags=re.I)
    ]
    result = []
    for token in tokens:
        if 1 < len(token) <= 24 and token.lower() not in {"use", "when", "this", "skill", "description"}:
            result.append(token)
    return _dedupe(result)


def _extract_command_lines(content: str, limit: int = 12) -> List[str]:
    commands: List[str] = []
    in_code = False
    for raw in _strip_frontmatter(content).splitlines():
        line = raw.strip()
        if line.startswith("```"):
            in_code = not in_code
            continue
        if not line:
            continue
        candidate = line[2:].strip() if line.startswith(("- ", "* ")) else line
        lower = candidate.lower()
        looks_like_command = (
            in_code
            or candidate.startswith(("/", "cow ", "python ", "python3 ", ".venv", "$env:", "高德 ", "查询", "帮我"))
            or ".py " in lower
            or " --" in candidate
        )
        if looks_like_command and 4 <= len(candidate) <= 220:
            commands.append(candidate)
        if len(commands) >= limit:
            break
    return _dedupe(commands)


def _extract_relevant_sections(body: str, max_chars: int = 6500) -> str:
    keywords = (
        "summary", "overview", "usage", "how to use", "commands", "examples", "when to use",
        "workflow", "safety", "configuration", "api", "功能", "用法", "使用", "命令", "示例",
        "触发", "流程", "配置", "安全", "输出", "限制", "错误", "高级",
    )
    lines = body.splitlines()
    chunks: List[str] = []
    index = 0
    while index < len(lines):
        line = lines[index]
        match = re.match(r"^(#{1,3})\s+(.+?)\s*$", line)
        if not match:
            index += 1
            continue
        level = len(match.group(1))
        title = match.group(2).strip()
        normalized_title = title.lower()
        if any(keyword in normalized_title for keyword in keywords):
            chunk = [line.rstrip()]
            index += 1
            while index < len(lines):
                next_line = lines[index]
                next_match = re.match(r"^(#{1,3})\s+", next_line)
                if next_match and len(next_match.group(1)) <= level:
                    break
                chunk.append(next_line.rstrip())
                index += 1
                if _text_size(chunk) >= 1600:
                    break
            chunks.append("\n".join(chunk).strip())
        else:
            index += 1
        if _text_size(chunks) >= max_chars:
            break
    return "\n\n".join(chunk for chunk in chunks if chunk).strip()[:max_chars]


def _dedupe(values: Iterable[str]) -> List[str]:
    seen = set()
    result = []
    for value in values:
        if value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


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
