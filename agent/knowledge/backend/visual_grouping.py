"""Visual artifact grouping helpers for multi-page figures and tables."""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from dataclasses import replace
from typing import Any, Iterable, Mapping, Optional


CONTINUATION_RE = re.compile(
    r"\b(?:continued\s+on\s+next\s+page|continued\s+from\s+previous\s+page|continued|cont['\u2019]?d)\b|"
    r"\u7eed\u8868|\u7eed\u56fe|\u63a5\u4e0a\u9875|\u4e0b\u9875\u7ee7\u7eed|\u4e0a\u9875\u7eed|\u7eed",
    re.IGNORECASE,
)
CAPTION_RE = re.compile(
    r"\b(?P<kind>table|figure|fig\.?)\s+(?P<number>\d+(?:[-.]\d+)*)(?:[.:：\s-]+(?P<title>[^\n\r]{0,220}))?",
    re.IGNORECASE,
)
SECTION_HEADING_RE = re.compile(r"^\s*\d+(?:\.\d+){0,5}\s+\S+")


@dataclass(frozen=True)
class GroupProposal:
    group_id: str
    document_id: str
    version_id: str
    kb_id: str
    group_type: str
    title: str
    caption: str
    source_pages: list[int]
    confidence: float
    members: list[dict[str, Any]]
    evidence: list[str]

    def to_group_record(self) -> dict[str, Any]:
        return {
            "id": self.group_id,
            "document_id": self.document_id,
            "version_id": self.version_id,
            "kb_id": self.kb_id,
            "group_type": self.group_type,
            "title": self.title,
            "caption": self.caption,
            "source_pages": self.source_pages,
            "status": "pending",
            "confidence": self.confidence,
            "retrievable": 0,
            "result_json": {
                "continuation_evidence": self.evidence,
                "source_pages": self.source_pages,
            },
        }


class VisualArtifactGrouper:
    """Detect likely multi-page visual artifacts for one document window."""

    def __init__(self, storage: Any):
        self.storage = storage

    def update_groups_for_document(
        self,
        document_id: str,
        version_id: str,
        page_window: Optional[tuple[int, int]] = None,
    ) -> dict[str, Any]:
        if not document_id or not version_id:
            return {"groups": 0, "members": 0, "low_confidence": 0}
        page_start = page_end = None
        if page_window:
            start_page, end_page = page_window
            page_start = max(1, int(start_page) - 2)
            page_end = max(page_start, int(end_page) + 2)
        artifacts = self.storage.list_visual_artifacts(
            document_id=document_id,
            version_id=version_id,
            page_start=page_start,
            page_end=page_end,
            limit=2000,
        )
        proposals = self._propose_groups(artifacts)
        written_groups = written_members = low_confidence = 0
        for proposal in proposals:
            if proposal.confidence < 0.70:
                low_confidence += 1
                continue
            proposal = self._resolve_existing_group_id(proposal)
            self.storage.upsert_visual_artifact_group(proposal.to_group_record())
            written_groups += 1
            total_parts = len(proposal.members)
            for index, member in enumerate(proposal.members, start=1):
                role = _role_for_part(index, total_parts)
                confidence = min(float(member.get("confidence") or proposal.confidence), proposal.confidence)
                self.storage.add_visual_artifact_group_member(
                    proposal.group_id,
                    member["artifact_id"],
                    index,
                    int(member.get("page") or 0),
                    role,
                    confidence,
                )
                self.storage.mark_visual_artifact_group_membership(
                    member["artifact_id"],
                    proposal.group_id,
                    index,
                    role,
                    confidence,
                    group_retrievable=0,
                )
                written_members += 1
        return {"groups": written_groups, "members": written_members, "low_confidence": low_confidence}

    def _resolve_existing_group_id(self, proposal: GroupProposal) -> GroupProposal:
        resolver = getattr(self.storage, "resolve_visual_group_id_for_members", None)
        if not callable(resolver):
            return proposal
        member_ids = [str(member.get("artifact_id") or "") for member in proposal.members]
        group_id = resolver(
            document_id=proposal.document_id,
            version_id=proposal.version_id,
            member_artifact_ids=member_ids,
            preferred_group_id=proposal.group_id,
        )
        if group_id == proposal.group_id:
            return proposal
        return replace(proposal, group_id=group_id)

    def _propose_groups(self, artifacts: list[dict[str, Any]]) -> list[GroupProposal]:
        candidates = [
            _ArtifactView.from_row(row)
            for row in artifacts
            if row and not is_toc_or_list_page(str(row.get("page_text") or ""))
        ]
        candidates.sort(key=lambda item: (item.page, item.y0, item.artifact_id))
        explicit = self._explicit_caption_groups(candidates)
        explicit_ids = {member["artifact_id"] for proposal in explicit for member in proposal.members}
        inferred = self._inferred_adjacent_groups([item for item in candidates if item.artifact_id not in explicit_ids])
        return explicit + inferred

    def _explicit_caption_groups(self, artifacts: list["_ArtifactView"]) -> list[GroupProposal]:
        by_caption: dict[tuple[str, str], list[_ArtifactView]] = {}
        for artifact in artifacts:
            caption = artifact.caption or artifact.label
            parsed = parse_caption_identity(caption)
            if not parsed:
                continue
            key = (parsed["number"], normalize_caption_title(parsed.get("title") or caption))
            by_caption.setdefault(key, []).append(artifact)

        proposals: list[GroupProposal] = []
        for (_caption_number, _title_key), items in by_caption.items():
            for chain in _consecutive_artifact_chains(items):
                pages = sorted({item.page for item in chain})
                if len(chain) < 2 or not _has_adjacent_pages(pages):
                    continue
                ordered = sorted(chain, key=lambda value: (value.page, value.y0, value.artifact_id))
                first = ordered[0]
                parsed = parse_caption_identity(first.caption or first.label) or {}
                confidence = 0.92 if any(has_continuation_marker(item.text) for item in ordered) else 0.84
                evidence = ["same caption number/title"]
                if any(has_continuation_marker(item.text) for item in ordered):
                    evidence.append("explicit continuation marker")
                proposals.append(
                    GroupProposal(
                        group_id=stable_visual_group_id_for_caption(
                            first.document_id,
                            first.version_id,
                            str(parsed.get("number") or ""),
                            str(parsed.get("title") or first.caption or first.label),
                        ),
                        document_id=first.document_id,
                        version_id=first.version_id,
                        kb_id=first.kb_id,
                        group_type=_group_type(first.artifact_type),
                        title=str(parsed.get("title") or first.caption or first.label),
                        caption=first.caption,
                        source_pages=pages,
                        confidence=confidence,
                        members=[item.member(confidence) for item in ordered],
                        evidence=evidence,
                    )
                )
        return proposals

    def _inferred_adjacent_groups(self, artifacts: list["_ArtifactView"]) -> list[GroupProposal]:
        proposals: list[GroupProposal] = []
        by_page: dict[int, list[_ArtifactView]] = {}
        for artifact in artifacts:
            by_page.setdefault(artifact.page, []).append(artifact)
        pages = sorted(by_page)
        used: set[str] = set()
        for page in pages:
            for first in by_page.get(page, []):
                if first.artifact_id in used:
                    continue
                chain = [first]
                confidence = 0.0
                evidence: list[str] = []
                next_page = page + 1
                while next_page in by_page:
                    match = self._best_continuation(chain[-1], by_page[next_page])
                    if not match:
                        break
                    candidate, candidate_confidence, candidate_evidence = match
                    if candidate.artifact_id in used:
                        break
                    chain.append(candidate)
                    confidence = max(confidence, candidate_confidence)
                    evidence.extend(candidate_evidence)
                    next_page += 1
                if len(chain) < 2:
                    continue
                for item in chain:
                    used.add(item.artifact_id)
                first_page, last_page = chain[0].page, chain[-1].page
                confidence = confidence or 0.74
                if len(chain) >= 3:
                    confidence = min(0.88, confidence + 0.04)
                proposals.append(
                    GroupProposal(
                        group_id=stable_visual_group_id_for_inferred(
                            first.document_id,
                            first.version_id,
                            first.artifact_id,
                            _group_type(first.artifact_type),
                        ),
                        document_id=first.document_id,
                        version_id=first.version_id,
                        kb_id=first.kb_id,
                        group_type=_group_type(first.artifact_type),
                        title=first.caption or first.label or "",
                        caption=first.caption,
                        source_pages=[item.page for item in chain],
                        confidence=confidence,
                        members=[item.member(confidence) for item in chain],
                        evidence=_unique(evidence or ["adjacent visual blocks"]),
                    )
                )
        return proposals

    def _best_continuation(
        self,
        previous: "_ArtifactView",
        page_items: list["_ArtifactView"],
    ) -> Optional[tuple["_ArtifactView", float, list[str]]]:
        best: Optional[tuple[_ArtifactView, float, list[str]]] = None
        for current in page_items:
            confidence, evidence = _continuation_score(previous, current)
            if confidence < 0.70:
                continue
            if best is None or confidence > best[1]:
                best = (current, confidence, evidence)
        return best


@dataclass(frozen=True)
class _ArtifactView:
    artifact_id: str
    document_id: str
    version_id: str
    kb_id: str
    artifact_type: str
    page: int
    label: str
    caption: str
    bbox: dict[str, Any]
    result_json: dict[str, Any]
    text: str

    @property
    def y0(self) -> float:
        return float(self.bbox.get("y0", 0) or 0)

    @property
    def y1(self) -> float:
        return float(self.bbox.get("y1", 0) or 0)

    @property
    def page_height(self) -> float:
        return float(self.bbox.get("page_height", 0) or self.bbox.get("height", 0) or 792)

    def member(self, confidence: float) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "page": self.page,
            "confidence": confidence,
        }

    @classmethod
    def from_row(cls, row: Mapping[str, Any]) -> "_ArtifactView":
        result_json = row.get("result_json") if isinstance(row.get("result_json"), dict) else {}
        continuation = result_json.get("continuation") if isinstance(result_json.get("continuation"), dict) else {}
        continuation_evidence = continuation.get("evidence") if isinstance(continuation.get("evidence"), list) else []
        uncertain_fields = result_json.get("uncertain_fields") if isinstance(result_json.get("uncertain_fields"), list) else []
        text_parts = [
            row.get("caption") or "",
            row.get("label") or "",
            row.get("page_text") or "",
            row.get("context_before") or "",
            row.get("context_after") or "",
            str(result_json.get("caption") or ""),
            str(result_json.get("title") or ""),
            str(result_json.get("summary") or ""),
            str(result_json.get("structured_markdown") or ""),
            *[str(item) for item in continuation_evidence],
            *[str(item) for item in uncertain_fields],
        ]
        return cls(
            artifact_id=str(row.get("id") or ""),
            document_id=str(row.get("document_id") or ""),
            version_id=str(row.get("version_id") or ""),
            kb_id=str(row.get("kb_id") or "kb_default"),
            artifact_type=str(row.get("artifact_type") or "unknown"),
            page=int(row.get("page") or 0),
            label=str(row.get("label") or ""),
            caption=str(row.get("caption") or ""),
            bbox=dict(row.get("bbox") or {}),
            result_json=result_json,
            text="\n".join(part for part in text_parts if part),
        )


def stable_visual_group_id_for_caption(
    document_id: str,
    version_id: str,
    caption_number: str,
    normalized_caption_title: str,
) -> str:
    raw = f"{document_id}|{version_id}|{caption_number}|{normalize_caption_title(normalized_caption_title)}"
    return "visual_group_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def stable_visual_group_id_for_inferred(
    document_id: str,
    version_id: str,
    anchor_artifact_id: str,
    artifact_type: str,
) -> str:
    raw = f"{document_id}|{version_id}|inferred|{anchor_artifact_id}|{artifact_type}"
    return "visual_group_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def parse_caption_identity(text: str) -> Optional[dict[str, str]]:
    match = CAPTION_RE.search(str(text or ""))
    if not match:
        return None
    return {
        "kind": "table" if match.group("kind").lower().startswith("table") else "figure",
        "number": match.group("number"),
        "title": _strip_continuation(match.group("title") or ""),
    }


def normalize_caption_title(value: str) -> str:
    text = _strip_continuation(value)
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", " ", text.lower())
    return " ".join(text.split())[:160]


def has_continuation_marker(text: str) -> bool:
    return bool(CONTINUATION_RE.search(str(text or "")))


def is_toc_or_list_page(page_text: str) -> bool:
    lowered = (page_text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "table of contents",
            "list of figures",
            "list of tables",
            "revision history",
            "\u76ee\u5f55",
        )
    )


def _continuation_score(previous: _ArtifactView, current: _ArtifactView) -> tuple[float, list[str]]:
    if is_toc_or_list_page(current.text):
        return 0.0, []
    score = 0.0
    evidence: list[str] = []
    previous_caption = parse_caption_identity(previous.caption or previous.label)
    current_caption = parse_caption_identity(current.caption or current.label)
    if previous_caption and current_caption and previous_caption["number"] == current_caption["number"]:
        score += 0.86
        evidence.append("same caption number")
    elif previous_caption and not current_caption:
        score += 0.24
        evidence.append("current page has no strict caption")
    if has_continuation_marker(current.text) or has_continuation_marker(previous.text):
        score += 0.34
        evidence.append("explicit continuation marker")
    if _header_similarity(previous.text, current.text) >= 0.5:
        score += 0.25
        evidence.append("similar table headers")
    if _dense_table_like(previous.text) and _dense_table_like(current.text):
        score += 0.20
        evidence.append("dense table-like tokens")
    if _looks_like_body_continuation(previous, current):
        score += 0.24
        evidence.append("bottom-to-top visual block continuation")
    if _has_new_section_heading(current.text) and not has_continuation_marker(current.text):
        score -= 0.25
        evidence.append("new section heading reduces confidence")
    return max(0.0, min(0.95, score)), _unique(evidence)


def _looks_like_body_continuation(previous: _ArtifactView, current: _ArtifactView) -> bool:
    previous_height = max(previous.page_height, previous.y1 or 1)
    current_height = max(current.page_height, current.y1 or 1)
    previous_bottom = previous.y1 / previous_height > 0.82
    current_top = current.y0 / current_height < 0.20
    same_visual_type = _group_type(previous.artifact_type) == _group_type(current.artifact_type)
    return previous_bottom and current_top and same_visual_type


def _header_similarity(left: str, right: str) -> float:
    left_tokens = _header_tokens(left)
    right_tokens = _header_tokens(right)
    if not left_tokens or not right_tokens:
        return 0.0
    overlap = len(left_tokens & right_tokens)
    return overlap / max(len(left_tokens), len(right_tokens))


def _header_tokens(text: str) -> set[str]:
    tokens = {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_/-]{1,32}", str(text or "")[:1200])
        if token.lower()
        in {
            "signal",
            "direction",
            "description",
            "name",
            "type",
            "bit",
            "field",
            "layer",
            "module",
            "width",
            "value",
            "encoding",
            "meaning",
        }
    }
    return tokens


def _dense_table_like(text: str) -> bool:
    value = str(text or "")[:2000].lower()
    token_hits = len(_header_tokens(value))
    pipe_lines = sum(1 for line in value.splitlines()[:30] if "|" in line or "\t" in line)
    repeated_columns = len(re.findall(r"\b(?:signal|direction|description|name|type|field|bit|width|module|layer)\b", value))
    return token_hits >= 2 or pipe_lines >= 2 or repeated_columns >= 4


def _has_new_section_heading(text: str) -> bool:
    for line in str(text or "").splitlines()[:5]:
        if SECTION_HEADING_RE.match(line):
            return True
    return False


def _strip_continuation(value: str) -> str:
    text = CONTINUATION_RE.sub("", str(value or ""))
    text = re.sub(r"\(\s*\)", "", text)
    return text.strip(" .:-：()")


def _role_for_part(index: int, total_parts: int) -> str:
    if total_parts <= 1:
        return "single"
    if index == 1:
        return "first"
    if index == total_parts:
        return "last"
    return "middle"


def _has_adjacent_pages(pages: Iterable[int]) -> bool:
    values = sorted(set(int(page) for page in pages))
    return any(right - left == 1 for left, right in zip(values, values[1:]))


def _consecutive_artifact_chains(items: Iterable[_ArtifactView]) -> list[list[_ArtifactView]]:
    ordered = sorted(items, key=lambda value: (value.page, value.y0, value.artifact_id))
    chains: list[list[_ArtifactView]] = []
    current: list[_ArtifactView] = []
    previous_page: Optional[int] = None
    for item in ordered:
        if previous_page is not None and item.page - previous_page > 1:
            if current:
                chains.append(current)
            current = []
        current.append(item)
        previous_page = item.page
    if current:
        chains.append(current)
    return chains


def _group_type(artifact_type: str) -> str:
    text = str(artifact_type or "unknown").lower()
    if text in {"table", "bitfield"}:
        return "table" if text == "table" else "bitfield"
    if text in {"timing_diagram", "state_machine", "waveform", "chart", "flowchart"}:
        return text
    return "figure"


def _bbox_signature(bboxes: list[Mapping[str, Any]]) -> str:
    compact = [
        {
            "x0": round(float(bbox.get("x0", 0) or 0) / 10) * 10,
            "y0": round(float(bbox.get("y0", 0) or 0) / 10) * 10,
            "x1": round(float(bbox.get("x1", 0) or 0) / 10) * 10,
            "y1": round(float(bbox.get("y1", 0) or 0) / 10) * 10,
        }
        for bbox in bboxes
    ]
    return hashlib.sha256(json.dumps(compact, sort_keys=True).encode("utf-8")).hexdigest()[:16]


def _unique(values: Iterable[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        key = text.lower()
        if not text or key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result


def bbox_area(bbox: Mapping[str, Any]) -> float:
    """Return the positive area of a PDF-point bbox mapping."""

    x0, y0, x1, y1 = _coords(bbox)
    return max(0.0, x1 - x0) * max(0.0, y1 - y0)


def bbox_iou(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    """Intersection-over-union for two bbox mappings."""

    intersection = _intersection_area(a, b)
    union = bbox_area(a) + bbox_area(b) - intersection
    return intersection / union if union > 0 else 0.0


def bbox_overlap_ratio(inner: Mapping[str, Any], outer: Mapping[str, Any]) -> float:
    """Return the fraction of *inner* covered by *outer*."""

    area = bbox_area(inner)
    return _intersection_area(inner, outer) / area if area > 0 else 0.0


def bbox_coverage(inner: Mapping[str, Any], outer: Mapping[str, Any]) -> float:
    """Alias for callers that use coverage terminology."""

    return bbox_overlap_ratio(inner, outer)


def _intersection_area(a: Mapping[str, Any], b: Mapping[str, Any]) -> float:
    ax0, ay0, ax1, ay1 = _coords(a)
    bx0, by0, bx1, by1 = _coords(b)
    ix0, iy0 = max(ax0, bx0), max(ay0, by0)
    ix1, iy1 = min(ax1, bx1), min(ay1, by1)
    return max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)


def _coords(bbox: Mapping[str, Any]) -> tuple[float, float, float, float]:
    return (
        float(bbox.get("x0", 0) or 0),
        float(bbox.get("y0", 0) or 0),
        float(bbox.get("x1", 0) or 0),
        float(bbox.get("y1", 0) or 0),
    )
