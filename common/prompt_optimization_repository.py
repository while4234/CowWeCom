# encoding:utf-8

"""Shared prompt optimization skill repository helpers."""

from __future__ import annotations

import os
import random
import re
from pathlib import Path
from typing import Any, Optional

try:
    from common.log import logger
except Exception:  # pragma: no cover - standalone import fallback
    import logging

    logger = logging.getLogger(__name__)


DEFAULT_GROK_KEYWORD = "grokSfw"
DEFAULT_PREFERRED_PROBABILITY = 0.9


def resolve_prompt_optimization_skill_dir(configured: str | None = None) -> Optional[Path]:
    candidates = [
        configured,
        os.environ.get("IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR"),
        os.environ.get("PROMPT_OPTIMIZATION_SKILL_DIR"),
    ]
    try:
        from config import conf

        candidates.extend(
            [
                conf().get("image_prompt_optimization_skill_dir"),
                (conf().get("skill", {}).get("image-prompt-optimization", {}) or {}).get("root")
                if isinstance(conf().get("skill", {}), dict)
                else "",
            ]
        )
    except Exception:
        pass

    project_root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            str(project_root / "skills" / "image-prompt-optimization"),
            str(Path.cwd()),
            str(Path.cwd().parent),
            str(Path.cwd().parents[1]) if len(Path.cwd().parents) > 1 else "",
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate)).expanduser()
        resolved = _as_prompt_optimization_skill_dir(path)
        if resolved:
            return resolved.resolve()
    return None


def _as_prompt_optimization_skill_dir(path: Path) -> Optional[Path]:
    if path.name == "image-prompt-optimization" and (path / "SKILL.md").is_file():
        return path
    nested = path / "skills" / "image-prompt-optimization"
    if (nested / "SKILL.md").is_file():
        return nested
    sibling = path / "image-prompt-optimization"
    if (sibling / "SKILL.md").is_file():
        return sibling
    return None


def resolve_nano_banana_library_dir(configured: str | None = None) -> Optional[Path]:
    candidates = [
        configured,
        os.environ.get("SKILL_IMAGE_GENERATION_PROMPT_LIBRARY_DIR"),
        os.environ.get("IMAGE_PROMPT_LIBRARY_DIR"),
    ]
    try:
        from config import conf

        skill_cfg = conf().get("skill", {}) if isinstance(conf().get("skill", {}), dict) else {}
        candidates.extend(
            [
                conf().get("image_prompt_library_dir"),
                (skill_cfg.get("image-prompt-optimization", {}) or {}).get("prompt_library_dir"),
                (skill_cfg.get("image-generation", {}) or {}).get("prompt_library_dir"),
            ]
        )
    except Exception:
        pass

    skill_dir = resolve_prompt_optimization_skill_dir()
    project_root = Path(__file__).resolve().parents[1]
    if skill_dir:
        candidates.append(str(skill_dir / "references" / "nano-banana-pro"))
    candidates.extend(
        [
            str(project_root / "skills" / "image-prompt-optimization" / "references" / "nano-banana-pro"),
            str(project_root / "skills" / "image-generation" / "references" / "nano-banana-pro"),
            str(Path.cwd().parent / "references" / "nano-banana-pro"),
            str(Path.cwd() / "references" / "nano-banana-pro"),
            str(Path.cwd().parents[1] / "references" / "nano-banana-pro") if len(Path.cwd().parents) > 1 else "",
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate)).expanduser()
        if (path / "manifest.json").is_file():
            return path.resolve()
    return None


def resolve_grok_system_prompt_path(media_type: str) -> Optional[Path]:
    media = "video" if str(media_type or "").strip().lower() == "video" else "image"
    skill_dir = resolve_prompt_optimization_skill_dir()
    if not skill_dir:
        return None
    path = skill_dir / "templates" / f"grok_{media}_system_prompt.txt"
    return path if path.is_file() else None


def select_grok_prompt_fragments(
    prompt: str,
    *,
    limit: int = 4,
    preferred_probability: float = DEFAULT_PREFERRED_PROBABILITY,
    repositories_root: str | os.PathLike[str] | None = None,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    root = _resolve_repositories_root(repositories_root)
    repositories = _load_text_repositories(root)
    keyword = _first_matching_repository_keyword(prompt, repositories)
    cleaned_prompt = strip_repository_keywords(prompt, repositories.keys())
    if not keyword:
        return {
            "keyword": "",
            "keyword_hit": False,
            "cleaned_prompt": cleaned_prompt,
            "preferred_probability": preferred_probability,
            "fragments": [],
            "repositories_root": str(root) if root else "",
        }

    randomizer = rng or random.SystemRandom()
    preferred = repositories.get(keyword, [])
    other = [fragment for name, fragments in repositories.items() if name != keyword for fragment in fragments]
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _ in range(max(int(limit or 0), 0)):
        use_preferred = randomizer.random() < preferred_probability
        pool = preferred if use_preferred else other
        fallback = other if use_preferred else preferred
        fragment = _pick_fragment(pool, fallback, randomizer)
        if not fragment:
            continue
        key = f"{fragment.get('repository')}::{fragment.get('file')}::{fragment.get('line')}::{fragment.get('text')}"
        if key in seen:
            continue
        selected.append(fragment)
        seen.add(key)
    return {
        "keyword": keyword,
        "keyword_hit": True,
        "cleaned_prompt": cleaned_prompt,
        "preferred_probability": preferred_probability,
        "fragments": selected,
        "repositories_root": str(root) if root else "",
    }


def strip_repository_keywords(prompt: str, keywords: Any) -> str:
    text = str(prompt or "")
    for keyword in keywords or []:
        name = str(keyword or "").strip()
        if not name:
            continue
        text = re.sub(rf"(?i)(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _resolve_repositories_root(configured: str | os.PathLike[str] | None) -> Optional[Path]:
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_dir() else path
    skill_dir = resolve_prompt_optimization_skill_dir()
    if not skill_dir:
        return None
    return (skill_dir / "repositories").resolve()


def _load_text_repositories(root: Optional[Path]) -> dict[str, list[dict[str, Any]]]:
    if not root or not root.is_dir():
        return {}
    repositories: dict[str, list[dict[str, Any]]] = {}
    for repo_dir in sorted(path for path in root.iterdir() if path.is_dir()):
        fragments = _load_text_fragments(repo_dir)
        repositories[repo_dir.name] = fragments
    return repositories


def _load_text_fragments(repo_dir: Path) -> list[dict[str, Any]]:
    fragments: list[dict[str, Any]] = []
    for path in sorted(repo_dir.rglob("*.txt")):
        if path.name.startswith("."):
            continue
        try:
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                fragments.append(
                    {
                        "repository": repo_dir.name,
                        "file": str(path.relative_to(repo_dir)),
                        "line": line_no,
                        "text": text,
                    }
                )
        except Exception as exc:
            logger.debug("[PromptOptimization] failed to read %s: %s", path, exc)
    return fragments


def _first_matching_repository_keyword(prompt: str, repositories: dict[str, list[dict[str, Any]]]) -> str:
    text = str(prompt or "")
    for name in repositories:
        if re.search(rf"(?i)(?<![A-Za-z0-9_-]){re.escape(name)}(?![A-Za-z0-9_-])", text):
            return name
    if re.search(rf"(?i)(?<![A-Za-z0-9_-]){re.escape(DEFAULT_GROK_KEYWORD)}(?![A-Za-z0-9_-])", text):
        return DEFAULT_GROK_KEYWORD if DEFAULT_GROK_KEYWORD in repositories else ""
    return ""


def _pick_fragment(
    pool: list[dict[str, Any]],
    fallback: list[dict[str, Any]],
    randomizer: random.Random,
) -> dict[str, Any] | None:
    source = pool or fallback
    if not source:
        return None
    return dict(randomizer.choice(source))
