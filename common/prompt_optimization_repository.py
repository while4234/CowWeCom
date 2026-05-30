# encoding:utf-8

"""Shared prompt optimization skill repository helpers."""

from __future__ import annotations

import difflib
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


DEFAULT_GROK_KEYWORD = "grok"
IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME = "image-prompt-optimization"
GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME = "grok-image-prompt-optimization"
DEFAULT_PREFERRED_PROBABILITY = 0.9
NSFW_CATEGORY = "NSFW"
NSFW_PRIORITY_PROBABILITY = 0.9
NATIONALITY_RACE_FILE = "States/Nationality-Race.txt"
SAFE_CONTEXT_SUPPLEMENT_CATEGORIES = {"Background", "Styling", "Colors", "Materials"}
DETAIL_SAFE_CATEGORIES = SAFE_CONTEXT_SUPPLEMENT_CATEGORIES | {"Clothing", "Props", "Concepts"}
EAST_ASIAN_NATIONALITY_TERMS = {"asian", "east asian", "chinese", "han chinese", "japanese", "korean"}
CONFLICTING_IDENTITY_TERMS = {
    "blonde",
    "blue eyes",
    "green eyes",
    "hazel eyes",
    "caucasian",
    "european",
    "nordic",
    "scandinavian",
    "fair skin",
    "platinum hair",
    "white hair",
    "red hair",
    "auburn hair",
}
REFERENCE_UNSAFE_FRAGMENT_PATHS = (
    "NSFW/Fetishes/Body-Type.txt",
    "NSFW/Nudity/",
    "Body/",
    "States/",
)
REFERENCE_IMAGE_CONSTRAINT_TEXT = (
    "mandatory reference image identity constraint: preserve the reference subject's exact face, "
    "facial structure, original expression, gaze direction, skin texture/tone, hair, distinctive features, "
    "and general body proportions; do not add new ethnicity, eye color, hair color, age, body type, "
    "facial expression, or facial traits from random fragments"
)
NATIONALITY_ALIASES = (
    (("韩国", "韩国人", "韩系", "韩裔", "南韩", "korea", "south korean", "korean"), "korean"),
    (("中国", "中国人", "华人", "华裔", "汉族", "china", "chinese"), "chinese"),
    (("日本", "日本人", "日系", "日裔", "japan", "japanese"), "japanese"),
    (("东亚", "东亚人", "east asian"), "east asian"),
    (("亚洲", "亚洲人", "亚裔", "asian"), "asian"),
)


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
                (conf().get("skill", {}).get(IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME, {}) or {}).get("root")
                if isinstance(conf().get("skill", {}), dict)
                else "",
            ]
        )
    except Exception:
        pass

    project_root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            str(project_root / "skills" / IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME),
            str(Path.cwd()),
            str(Path.cwd().parent),
            str(Path.cwd().parents[1]) if len(Path.cwd().parents) > 1 else "",
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate)).expanduser()
        resolved = _as_skill_dir(path, IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME)
        if resolved:
            return resolved.resolve()
    return None


def resolve_grok_image_prompt_optimization_skill_dir(configured: str | None = None) -> Optional[Path]:
    candidates = [
        configured,
        os.environ.get("GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_DIR"),
    ]
    try:
        from config import conf

        skill_cfg = conf().get("skill", {}) if isinstance(conf().get("skill", {}), dict) else {}
        candidates.extend(
            [
                conf().get("grok_image_prompt_optimization_skill_dir"),
                (skill_cfg.get(GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME, {}) or {}).get("root"),
            ]
        )
    except Exception:
        pass

    project_root = Path(__file__).resolve().parents[1]
    candidates.extend(
        [
            str(project_root / "skills" / GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME),
            str(Path.cwd()),
            str(Path.cwd().parent),
            str(Path.cwd().parents[1]) if len(Path.cwd().parents) > 1 else "",
        ]
    )
    for candidate in candidates:
        if not candidate:
            continue
        path = Path(str(candidate)).expanduser()
        resolved = _as_skill_dir(path, GROK_IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME)
        if resolved:
            return resolved.resolve()
    return None


def _as_skill_dir(path: Path, skill_name: str) -> Optional[Path]:
    if path.name == skill_name and (path / "SKILL.md").is_file():
        return path
    nested = path / "skills" / skill_name
    if (nested / "SKILL.md").is_file():
        return nested
    sibling = path / skill_name
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
                (skill_cfg.get(IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME, {}) or {}).get("prompt_library_dir"),
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
            str(project_root / "skills" / IMAGE_PROMPT_OPTIMIZATION_SKILL_NAME / "references" / "nano-banana-pro"),
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
    if media == "image":
        skill_dir = resolve_grok_image_prompt_optimization_skill_dir()
        if not skill_dir:
            return None
        path = skill_dir / "templates" / "grok_image_system_prompt.txt"
    else:
        project_root = Path(__file__).resolve().parents[1]
        path = project_root / "skills" / "grok-video-generation" / "templates" / "grok_video_system_prompt.txt"
    return path if path.is_file() else None


def select_grok_prompt_fragments(
    prompt: str,
    *,
    limit: int = 4,
    preferred_probability: float = DEFAULT_PREFERRED_PROBABILITY,
    repositories_root: str | os.PathLike[str] | None = None,
    reference_image: bool = False,
    rng: random.Random | None = None,
) -> dict[str, Any]:
    root = _resolve_grok_repositories_root(repositories_root)
    repositories = _load_text_repositories(root)
    explicit_keyword = _first_matching_repository_keyword(prompt, repositories)
    nsfw_priority = _contains_nsfw_keyword(prompt) and DEFAULT_GROK_KEYWORD in repositories
    keyword = DEFAULT_GROK_KEYWORD if nsfw_priority else (explicit_keyword or _default_grok_repository(repositories))
    priority_category = NSFW_CATEGORY if nsfw_priority else ""
    cleaned_prompt = strip_control_keywords(strip_repository_keywords(prompt, repositories.keys()))
    if not keyword:
        return {
            "keyword": "",
            "keyword_hit": False,
            "category": priority_category,
            "category_forced": False,
            "category_priority": bool(priority_category),
            "selection_mode": "none",
            "cleaned_prompt": cleaned_prompt,
            "preferred_probability": preferred_probability,
            "fragment_prompt": "",
            "fragments": [],
            "repositories_root": str(root) if root else "",
        }

    randomizer = rng or random.SystemRandom()
    preferred = repositories.get(keyword, [])
    constraints = _select_stable_constraints(prompt, preferred, reference_image=reference_image)
    if priority_category:
        priority_all = _filter_fragments_by_category(preferred, priority_category)
        priority = _filter_fragments_by_query(priority_all, cleaned_prompt) or priority_all
        if _has_reference_image_constraint(constraints):
            priority = _filter_reference_safe_priority_fragments(priority)
        non_priority_preferred = [
            fragment for fragment in preferred if not _fragment_category_matches(fragment, priority_category)
        ]
        safe_supplements = _filter_safe_context_supplements(non_priority_preferred, constraints=constraints)
        other = _filter_fragments_by_query(safe_supplements, cleaned_prompt) or safe_supplements
        selected = _select_priority_fragments(
            priority,
            other,
            limit=max(int(limit or 0), 0),
            randomizer=randomizer,
        )
        selection_mode = "priority_with_supplement"
    else:
        other = [fragment for name, fragments in repositories.items() if name != keyword for fragment in fragments]
        if constraints:
            preferred = _filter_detail_safe_fragments(preferred, constraints=constraints)
            other = _filter_detail_safe_fragments(other, constraints=constraints)
        filtered_preferred = _filter_fragments_by_query(preferred, cleaned_prompt)
        filtered_other = _filter_fragments_by_query(other, cleaned_prompt)
        if filtered_preferred:
            preferred = filtered_preferred
        if filtered_other:
            other = filtered_other
        selected = _select_weighted_fragments(
            preferred,
            other,
            limit=max(int(limit or 0), 0),
            preferred_probability=preferred_probability,
            preferred_repository=keyword,
            randomizer=randomizer,
        )
        selection_mode = "weighted_repository"
    return {
        "keyword": keyword,
        "keyword_hit": bool(explicit_keyword and explicit_keyword == keyword),
        "category": priority_category,
        "category_forced": bool(priority_category),
        "category_priority": bool(priority_category),
        "category_exclusive": False,
        "selection_mode": selection_mode,
        "cleaned_prompt": cleaned_prompt,
        "preferred_probability": preferred_probability,
        "constraints": constraints,
        "fragment_prompt": _compose_fragment_prompt(constraints + selected),
        "fragments": constraints + selected,
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


def strip_control_keywords(prompt: str) -> str:
    text = str(prompt or "")
    text = re.sub(r"(?i)(?<![A-Za-z0-9_-])NSFW(?![A-Za-z0-9_-])", " ", text)
    text = text.replace("大尺度", " ")
    return re.sub(r"\s+", " ", text).strip()


def _resolve_grok_repositories_root(configured: str | os.PathLike[str] | None) -> Optional[Path]:
    if configured:
        path = Path(configured).expanduser()
        return path.resolve() if path.is_dir() else path
    skill_dir = resolve_grok_image_prompt_optimization_skill_dir()
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
            relative_path = path.relative_to(repo_dir)
            relative_name = relative_path.as_posix()
            category = relative_path.parts[0] if len(relative_path.parts) > 1 else ""
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                text = line.strip()
                if not text or text.startswith("#"):
                    continue
                fragments.append(
                    {
                        "repository": repo_dir.name,
                        "category": category,
                        "file": relative_name,
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
    return ""


def _default_grok_repository(repositories: dict[str, list[dict[str, Any]]]) -> str:
    return DEFAULT_GROK_KEYWORD if DEFAULT_GROK_KEYWORD in repositories else ""


def _contains_nsfw_keyword(prompt: str) -> bool:
    text = str(prompt or "")
    return bool(re.search(r"(?i)(?<![A-Za-z0-9_-])NSFW(?![A-Za-z0-9_-])", text) or "大尺度" in text)


def _filter_fragments_by_category(fragments: list[dict[str, Any]], category: str) -> list[dict[str, Any]]:
    expected = str(category or "").strip().lower()
    return [fragment for fragment in fragments if _fragment_category_matches(fragment, expected)]


def _filter_safe_context_supplements(
    fragments: list[dict[str, Any]],
    *,
    constraints: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        fragment
        for fragment in fragments
        if _fragment_category(fragment) in SAFE_CONTEXT_SUPPLEMENT_CATEGORIES
        and not _fragment_conflicts_with_constraints(fragment, constraints)
    ]


def _filter_detail_safe_fragments(
    fragments: list[dict[str, Any]],
    *,
    constraints: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    return [
        fragment
        for fragment in fragments
        if _fragment_category(fragment) in DETAIL_SAFE_CATEGORIES
        and not _fragment_conflicts_with_constraints(fragment, constraints)
    ]


def _filter_fragments_by_query(fragments: list[dict[str, Any]], prompt: str) -> list[dict[str, Any]]:
    terms = _prompt_query_terms(prompt)
    if not terms:
        return fragments
    scored: list[tuple[float, dict[str, Any]]] = []
    for fragment in fragments:
        score = _query_match_score(terms, _fragment_search_text(fragment))
        if score >= 1.0:
            scored.append((score, fragment))
    if not scored:
        return []
    scored.sort(key=lambda item: item[0], reverse=True)
    return [fragment for _, fragment in scored]


def _prompt_query_terms(prompt: str) -> list[str]:
    text = strip_control_keywords(str(prompt or "")).lower()
    text = re.sub(
        r"(?i)\b(random|generate|create|make|draw|image|picture|photo|prompt|grok|xai|please|with|about)\b",
        " ",
        text,
    )
    for word in ("随机", "生成", "提示词", "图片", "照片", "画图", "生图", "的", "一个", "一张"):
        text = text.replace(word, " ")
    terms = re.findall(r"[a-z][a-z0-9_-]{2,}|[\u4e00-\u9fff]{2,}", text)
    return list(dict.fromkeys(term.strip() for term in terms if term.strip()))[:12]


def _fragment_search_text(fragment: dict[str, Any]) -> str:
    return " ".join(
        str(fragment.get(key) or "")
        for key in ("repository", "category", "file", "text")
    ).lower().replace("-", " ").replace("_", " ")


def _query_match_score(terms: list[str], haystack: str) -> float:
    score = 0.0
    haystack_terms = re.findall(r"[a-z][a-z0-9]+|[\u4e00-\u9fff]{2,}", haystack)
    for term in terms:
        if term in haystack:
            score += 2.0
            continue
        if re.search(r"[\u4e00-\u9fff]", term):
            continue
        best = max((difflib.SequenceMatcher(None, term, candidate).ratio() for candidate in haystack_terms), default=0.0)
        if best >= 0.82:
            score += best
    return score


def _filter_reference_safe_priority_fragments(fragments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        fragment
        for fragment in fragments
        if not _fragment_path_matches(fragment, REFERENCE_UNSAFE_FRAGMENT_PATHS)
        and not _fragment_conflicts_with_constraints(fragment, [_reference_image_constraint()])
    ]


def _fragment_category(fragment: dict[str, Any]) -> str:
    return str(fragment.get("category") or "").strip()


def _fragment_path_matches(fragment: dict[str, Any], prefixes: tuple[str, ...]) -> bool:
    name = str(fragment.get("file") or "").replace("\\", "/")
    return any(name == prefix.rstrip("/") or name.startswith(prefix) for prefix in prefixes)


def _fragment_conflicts_with_constraints(
    fragment: dict[str, Any],
    constraints: list[dict[str, Any]] | None,
) -> bool:
    if not constraints or not _has_identity_constraint(constraints):
        return False
    text = str(fragment.get("text") or "").lower()
    return any(term in text for term in CONFLICTING_IDENTITY_TERMS)


def _has_identity_constraint(constraints: list[dict[str, Any]] | None) -> bool:
    return any(
        str(fragment.get("constraint_type") or "").strip().lower()
        in {"nationality", "reference_image_identity"}
        for fragment in constraints or []
    )


def _has_reference_image_constraint(constraints: list[dict[str, Any]] | None) -> bool:
    return any(
        str(fragment.get("constraint_type") or "").strip().lower() == "reference_image_identity"
        for fragment in constraints or []
    )


def _fragment_category_matches(fragment: dict[str, Any], category: str) -> bool:
    expected = str(category or "").strip().lower()
    return bool(expected) and str(fragment.get("category") or "").strip().lower() == expected


def _select_stable_constraints(
    prompt: str,
    preferred: list[dict[str, Any]],
    *,
    reference_image: bool = False,
) -> list[dict[str, Any]]:
    if reference_image:
        return [_reference_image_constraint()]
    nationality = _find_nationality_constraint(prompt, preferred)
    return [nationality] if nationality else []


def _reference_image_constraint() -> dict[str, Any]:
    return {
        "repository": "grok",
        "category": "Reference",
        "file": "reference-image",
        "line": 0,
        "text": REFERENCE_IMAGE_CONSTRAINT_TEXT,
        "selection_role": "constraint",
        "constraint_type": "reference_image_identity",
    }


def _find_nationality_constraint(prompt: str, preferred: list[dict[str, Any]]) -> dict[str, Any] | None:
    nationality_fragments = [
        fragment
        for fragment in preferred
        if str(fragment.get("file") or "").replace("\\", "/") == NATIONALITY_RACE_FILE
    ]
    if not nationality_fragments:
        return None

    matched_term = _matched_nationality_term(prompt, nationality_fragments)
    if not matched_term:
        return None
    for fragment in nationality_fragments:
        if str(fragment.get("text") or "").strip().lower() == matched_term:
            constraint = dict(fragment)
            constraint["selection_role"] = "constraint"
            constraint["constraint_type"] = "nationality"
            constraint["source_text"] = fragment.get("text")
            constraint["text"] = _nationality_constraint_text(matched_term)
            return constraint
    return None


def _matched_nationality_term(prompt: str, nationality_fragments: list[dict[str, Any]]) -> str:
    text = str(prompt or "")
    for aliases, repository_term in NATIONALITY_ALIASES:
        if any(_prompt_contains_term(text, alias) for alias in aliases):
            return repository_term

    repository_terms = sorted(
        {str(fragment.get("text") or "").strip().lower() for fragment in nationality_fragments},
        key=len,
        reverse=True,
    )
    for term in repository_terms:
        if term and _prompt_contains_term(text, term):
            return term
    return ""


def _prompt_contains_term(prompt: str, term: str) -> bool:
    text = str(prompt or "")
    value = str(term or "").strip()
    if not value:
        return False
    if re.search(r"[A-Za-z0-9]", value):
        return bool(re.search(rf"(?i)(?<![A-Za-z0-9_-]){re.escape(value)}(?![A-Za-z0-9_-])", text))
    return value in text


def _nationality_constraint_text(term: str) -> str:
    normalized = str(term or "").strip().lower()
    if normalized in EAST_ASIAN_NATIONALITY_TERMS:
        identity_label = "Korean/East Asian" if normalized == "korean" else f"{normalized}/East Asian"
        return (
            f"mandatory nationality/ethnicity constraint: {normalized}; keep the subject's appearance "
            f"consistent with this request, with {identity_label} facial features, natural dark hair, "
            "and dark brown eyes unless the user explicitly requests otherwise; do not add conflicting "
            "identity traits from random fragments"
        )
    return (
        f"mandatory nationality/ethnicity constraint: {normalized}; keep facial features, styling, "
        "and cultural/national identity consistent with this request; do not add conflicting identity "
        "traits from random fragments"
    )


def _select_weighted_fragments(
    preferred: list[dict[str, Any]],
    other: list[dict[str, Any]],
    *,
    limit: int,
    preferred_probability: float,
    preferred_repository: str,
    randomizer: random.Random,
) -> list[dict[str, Any]]:
    selected: list[dict[str, Any]] = []
    seen: set[str] = set()
    for _ in range(limit):
        use_preferred = randomizer.random() < preferred_probability
        pool = preferred if use_preferred else other
        fallback = other if use_preferred else preferred
        fragment = _pick_fragment(pool, fallback, randomizer, seen=seen)
        if not fragment:
            continue
        fragment["selection_role"] = (
            "preferred" if str(fragment.get("repository") or "") == str(preferred_repository or "") else "supplement"
        )
        selected.append(fragment)
        seen.add(_fragment_key(fragment))
    return selected


def _select_priority_fragments(
    priority: list[dict[str, Any]],
    supplement: list[dict[str, Any]],
    *,
    limit: int,
    randomizer: random.Random,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []

    selected: list[dict[str, Any]] = []
    seen: set[str] = set()

    for _ in range(limit):
        use_priority = bool(priority) and (not supplement or randomizer.random() < NSFW_PRIORITY_PROBABILITY)
        pool = priority if use_priority else supplement
        fallback = supplement if use_priority else priority
        fragment = _pick_fragment(pool, fallback, randomizer, seen=seen)
        if not fragment:
            continue
        fragment["selection_role"] = "priority" if _fragment_category_matches(fragment, NSFW_CATEGORY) else "supplement"
        selected.append(fragment)
        seen.add(_fragment_key(fragment))
        if not (_unseen_fragments(priority, seen) or _unseen_fragments(supplement, seen)):
            break
    return selected


def _pick_fragment(
    pool: list[dict[str, Any]],
    fallback: list[dict[str, Any]],
    randomizer: random.Random,
    *,
    seen: set[str] | None = None,
) -> dict[str, Any] | None:
    source = _unseen_fragments(pool, seen) or _unseen_fragments(fallback, seen)
    if not source:
        return None
    return dict(randomizer.choice(source))


def _unseen_fragments(fragments: list[dict[str, Any]], seen: set[str] | None) -> list[dict[str, Any]]:
    if not seen:
        return fragments
    return [fragment for fragment in fragments if _fragment_key(fragment) not in seen]


def _fragment_key(fragment: dict[str, Any]) -> str:
    return f"{fragment.get('repository')}::{fragment.get('file')}::{fragment.get('line')}::{fragment.get('text')}"


def _compose_fragment_prompt(fragments: list[dict[str, Any]]) -> str:
    lines = []
    for fragment in fragments:
        role = str(fragment.get("selection_role") or "fragment")
        lines.append(f"- {role}: {fragment.get('text')}")
    return "\n".join(lines)
