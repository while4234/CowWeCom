"""Vision-model analysis and chunk conversion for visual knowledge artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from common.log import logger

from .models import KnowledgeChunk, KnowledgeDocument, SourceSpan, VisualAnalysisResult, VisualArtifactCandidate
from .storage import stable_visual_chunk_id, stable_visual_group_chunk_id, stable_visual_group_span_id, stable_visual_span_id


SYSTEM_PROMPT = (
    "你是技术文档视觉元素解析器。你的任务是把技术文档中的技术图、表格、时序图、状态机、"
    "位域图、流程图、截图、代码图、架构图和嵌入图片解析为可用于 RAG 检索的高质量结构化内容。"
    "必须严格基于给定图片和上下文，不得猜测。无法确认的内容写入 uncertain_fields。"
    "图片模糊、文字不可辨、结构不清时降低 confidence，并把 should_index 设为 false。"
    "只输出合法 JSON，不要输出 Markdown fence。"
)


OUTPUT_SCHEMA = {
    "artifact_type": "table|figure|chart|timing_diagram|state_machine|waveform|bitfield|flowchart|image|unknown",
    "title": "",
    "caption": "",
    "page": 0,
    "summary": "",
    "structured_markdown": "",
    "key_facts": [{"fact": "", "confidence": 0.0}],
    "table": {"headers": [], "rows": [], "markdown": "", "html": ""},
    "signals": [{"name": "", "direction": "", "width": "", "meaning": "", "confidence": 0.0}],
    "state_machine": {
        "states": [],
        "transitions": [{"from": "", "to": "", "condition": "", "action": "", "confidence": 0.0}],
    },
    "chart": {"axes": [], "series": [], "observations": []},
    "uncertain_fields": [],
    "readability": "good|medium|poor",
    "confidence": {"ocr": 0.0, "structure": 0.0, "semantic": 0.0, "overall": 0.0},
    "is_partial": False,
    "continuation": {
        "role": "single|first|middle|last|unknown",
        "belongs_to_same_artifact": False,
        "evidence": [],
        "confidence": 0.0,
    },
    "should_index": True,
    "low_confidence_reason": "",
}


GROUP_OUTPUT_SCHEMA = {
    "artifact_type": "table|figure|chart|timing_diagram|state_machine|waveform|bitfield|flowchart|image|unknown",
    "title": "",
    "caption": "",
    "is_multipage": True,
    "source_pages": [],
    "summary": "",
    "structured_markdown": "",
    "key_facts": [{"fact": "", "confidence": 0.0}],
    "parts": [{"page": 0, "artifact_id": "", "role": "", "summary": "", "confidence": 0.0}],
    "merged_table": {"headers": [], "rows": [], "markdown": "", "html": "", "row_page_map": []},
    "continuation_evidence": [],
    "uncertain_continuations": [],
    "confidence": {
        "ocr": 0.0,
        "structure": 0.0,
        "semantic": 0.0,
        "continuation": 0.0,
        "overall": 0.0,
    },
    "should_index": True,
}


class VisualAnalyzer:
    """Analyze one visual artifact via the configured OpenAI-compatible vision path."""

    supports_group_vision_merge = True

    def analyze(
        self,
        candidate: VisualArtifactCandidate,
        config: Any,
        document: KnowledgeDocument | None = None,
        *,
        analysis_backend: Optional[str] = None,
    ) -> VisualAnalysisResult:
        visual_config = getattr(config, "visual_analysis", {}) or {}
        content = self._call_model(candidate, config, document, analysis_backend=analysis_backend)
        return validate_visual_analysis_json(content, candidate, visual_config)

    def analyze_group(
        self,
        group: Dict[str, Any],
        members: List[Dict[str, Any]],
        config: Any,
        document: KnowledgeDocument | None = None,
        *,
        analysis_backend: Optional[str] = None,
    ) -> Dict[str, Any]:
        visual_config = getattr(config, "visual_analysis", {}) or {}
        content = self._call_group_model(group, members, config, document, analysis_backend=analysis_backend)
        return validate_visual_group_analysis_json(content, group, members, visual_config)

    def _call_model(
        self,
        candidate: VisualArtifactCandidate,
        config: Any,
        document: KnowledgeDocument | None,
        *,
        analysis_backend: Optional[str],
    ) -> str:
        from models.openai.open_ai_bot import OpenAIBot

        visual_config = getattr(config, "visual_analysis", {}) or {}
        model = str(visual_config.get("model") or "gpt-5.5")
        reasoning_effort = str(visual_config.get("reasoning_effort") or "xhigh")
        effective_backend = resolve_visual_analysis_backend(
            analysis_backend if analysis_backend is not None else visual_config.get("analysis_backend")
        )
        bot: Any
        if effective_backend == "codex":
            from models.codex.codex_bot import CodexBot

            bot = CodexBot()
        else:
            bot = OpenAIBot(backend_override=effective_backend)
        prompt = build_visual_prompt(candidate, document, visual_config)
        image_url = image_file_to_data_url(candidate.image_path, int(visual_config.get("max_image_long_edge", 1800) or 1800))
        response = bot.call_vision(
            image_url=image_url,
            question=f"{SYSTEM_PROMPT}\n\n{prompt}",
            model=model,
            max_tokens=int(visual_config.get("max_output_tokens", 3000) or 3000),
            reasoning_effort=reasoning_effort,
            reasoning_effort_locked=True,
        )
        if isinstance(response, dict) and response.get("error"):
            raise RuntimeError(str(response.get("message") or "vision model request failed"))
        content = response.get("content", "") if isinstance(response, dict) else str(response or "")
        if not str(content).strip():
            raise RuntimeError("vision model response was empty")
        return str(content).strip()

    def _call_group_model(
        self,
        group: Dict[str, Any],
        members: List[Dict[str, Any]],
        config: Any,
        document: KnowledgeDocument | None,
        *,
        analysis_backend: Optional[str],
    ) -> str:
        try:
            return self._call_group_vision_model(
                group,
                members,
                config,
                document,
                analysis_backend=analysis_backend,
            )
        except Exception as exc:
            logger.debug("[KnowledgeBackend] visual group vision merge unavailable: %s", exc)
            fallback = merge_visual_group_from_member_results(group, members)
            return json.dumps(fallback, ensure_ascii=False)

    def _call_group_vision_model(
        self,
        group: Dict[str, Any],
        members: List[Dict[str, Any]],
        config: Any,
        document: KnowledgeDocument | None,
        *,
        analysis_backend: Optional[str],
    ) -> str:
        from models.openai.open_ai_bot import OpenAIBot

        visual_config = getattr(config, "visual_analysis", {}) or {}
        model = str(visual_config.get("model") or "gpt-5.5")
        reasoning_effort = str(visual_config.get("reasoning_effort") or "xhigh")
        max_pages = int(visual_config.get("group_model_merge_max_pages") or 4)
        ordered_members = sorted(members, key=lambda item: int(item.get("part_index") or 0))
        if len(ordered_members) > max_pages:
            raise RuntimeError("visual group has too many pages for model vision merge")
        effective_backend = resolve_visual_analysis_backend(
            analysis_backend if analysis_backend is not None else visual_config.get("analysis_backend")
        )
        if effective_backend == "codex":
            raise RuntimeError("codex backend does not expose a multi-image group wrapper yet")
        else:
            bot = OpenAIBot(backend_override=effective_backend)
        prompt = build_visual_group_prompt(group, members, document, visual_config)
        max_long_edge = int(
            visual_config.get("group_max_image_long_edge")
            or visual_config.get("max_image_long_edge_high_res")
            or visual_config.get("max_image_long_edge")
            or 1800
        )
        content: List[Dict[str, Any]] = [{"type": "text", "text": f"{SYSTEM_PROMPT}\n\n{prompt}"}]
        for index, member in enumerate(ordered_members, start=1):
            image_path = str(member.get("image_path") or "").strip()
            if not image_path:
                raise FileNotFoundError(f"visual group member image missing: {member.get('artifact_id') or index}")
            content.append(
                {
                    "type": "text",
                    "text": (
                        f"Part {index}: page={member.get('page')}, "
                        f"artifact_id={member.get('artifact_id')}, role={member.get('role') or ''}"
                    ),
                }
            )
            content.append(
                {
                    "type": "image_url",
                    "image_url": {"url": image_file_to_data_url(image_path, max_long_edge)},
                }
            )
        response = bot.call_with_tools(
            [{"role": "user", "content": content}],
            tools=None,
            stream=False,
            model=model,
            max_tokens=int(visual_config.get("group_max_output_tokens", visual_config.get("max_output_tokens", 3000)) or 3000),
            temperature=0,
            reasoning_effort=reasoning_effort,
            reasoning_effort_locked=True,
            request_timeout=int(visual_config.get("group_timeout_seconds", visual_config.get("timeout_seconds", 300)) or 300),
        )
        if isinstance(response, dict) and response.get("error"):
            raise RuntimeError(str(response.get("message") or "visual group model request failed"))
        content = _chat_completion_content(response)
        if not content.strip():
            raise RuntimeError("visual group model response was empty")
        return content.strip()


def normalize_visual_analysis_backend(value: Any) -> str:
    if value is None:
        return "current"
    text = str(value or "").strip().lower()
    if text in ("", "current"):
        return "current"
    if text in ("capi", "capi_monthly", "codex"):
        return text
    if text in ("capi-monthly", "capi_month", "capi-month", "monthly", "month"):
        return "capi_monthly"
    raise ValueError(f"unsupported visual analysis backend: {value}")


def resolve_visual_analysis_backend(value: Any) -> str:
    requested = normalize_visual_analysis_backend(value)
    if requested == "current":
        from common.llm_backend_router import get_current_backend

        return normalize_visual_analysis_backend(get_current_backend())
    return requested


def build_visual_prompt(candidate: VisualArtifactCandidate, document: KnowledgeDocument | None, visual_config: Dict[str, Any]) -> str:
    page_text = candidate.page_text[:3000] if visual_config.get("include_page_context", True) else ""
    payload = {
        "document_title": document.title if document else "",
        "document_id": candidate.document_id,
        "version_id": candidate.version_id,
        "page": candidate.page,
        "artifact_type_candidate": candidate.artifact_type,
        "caption": candidate.caption,
        "label": candidate.label,
        "section_path": candidate.section_path,
        "context_before": candidate.context_before[: int(visual_config.get("context_before_chars", 1200) or 1200)],
        "context_after": candidate.context_after[: int(visual_config.get("context_after_chars", 1200) or 1200)],
        "page_text_excerpt": page_text,
        "parser_confidence": candidate.parser_confidence,
        "bbox": candidate.bbox,
        "output_schema": OUTPUT_SCHEMA,
    }
    instruction = (
        "Analyze this document visual artifact. Return strict JSON only.\n"
        "该视觉元素可能是跨页图表的一部分。不要假设当前图片包含完整图表。"
        "若看到 continued、续表、缺少表头、页首续接、页尾未结束等迹象，请在 continuation 字段中说明。"
        "If headers are missing, infer only when the supplied context supports it and list uncertainty in uncertain_fields.\n\n"
    )
    if visual_config.get("prompt_mode") == "tile":
        instruction += (
            "Tile mode: this image is a local tile of a large visual artifact. "
            "Do not treat it as the complete table or figure. "
            "Only parse visible content, preserve tile_index/visible_range attribution, "
            "and do not fabricate missing headers without supplied context.\n\n"
        )
    return instruction + json.dumps(payload, ensure_ascii=False, indent=2)


def build_visual_group_prompt(
    group: Dict[str, Any],
    members: List[Dict[str, Any]],
    document: KnowledgeDocument | None,
    visual_config: Dict[str, Any],
) -> str:
    ordered = sorted(members, key=lambda item: int(item.get("part_index") or 0))
    payload = {
        "document_title": document.title if document else "",
        "document_id": group.get("document_id") or "",
        "version_id": group.get("version_id") or "",
        "group_id": group.get("id") or "",
        "group_type_candidate": group.get("group_type") or "unknown",
        "title": group.get("title") or "",
        "caption": group.get("caption") or "",
        "source_pages": group.get("source_pages") or [],
        "continuation_confidence_candidate": group.get("confidence") or 0,
        "members": [_group_member_prompt_payload(member, visual_config) for member in ordered],
        "output_schema": GROUP_OUTPUT_SCHEMA,
    }
    instruction = (
        "你正在合并同一个多页图表/表格的多个部分。必须保持 page attribution。"
        "不得补造缺失行列。若不同页的表头或结构不一致，降低 confidence。"
        "输出应包含 source_pages、parts、continuation_evidence 和 uncertain_continuations。"
        "If any page-level result is low confidence, cap the group confidence and explain it in uncertain_continuations. "
        "Return strict JSON only.\n\n"
    )
    return instruction + json.dumps(payload, ensure_ascii=False, indent=2)


def validate_visual_analysis_json(
    raw: Any,
    candidate: VisualArtifactCandidate,
    visual_config: Dict[str, Any],
) -> VisualAnalysisResult:
    if isinstance(raw, VisualAnalysisResult):
        data = raw.to_dict()
    elif isinstance(raw, dict):
        data = dict(raw)
    else:
        text = str(raw or "").strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"visual analysis response is not valid JSON: {exc}") from exc

    confidence = data.get("confidence")
    if not isinstance(confidence, dict):
        raise ValueError("visual analysis JSON is missing confidence")
    normalized_confidence = {
        "ocr": _clamp01(confidence.get("ocr", 0)),
        "structure": _clamp01(confidence.get("structure", 0)),
        "semantic": _clamp01(confidence.get("semantic", 0)),
        "overall": _clamp01(confidence.get("overall", 0)),
    }
    explicit_should_index = bool(data.get("should_index", False))
    reasons: List[str] = []
    if data.get("readability") == "poor":
        reasons.append("readability is poor")
    thresholds = {
        "overall": float(visual_config.get("min_confidence", 0.78)),
        "ocr": float(visual_config.get("min_ocr_confidence", 0.70)),
        "structure": float(visual_config.get("min_structure_confidence", 0.75)),
        "semantic": float(visual_config.get("min_semantic_confidence", 0.75)),
    }
    for key, threshold in thresholds.items():
        if normalized_confidence[key] < threshold:
            reasons.append(f"{key} confidence {normalized_confidence[key]:.2f} below {threshold:.2f}")
    artifact_type = str(data.get("artifact_type") or candidate.artifact_type or "unknown")
    table = data.get("table") if isinstance(data.get("table"), dict) else {}
    structured_markdown = str(data.get("structured_markdown") or "")
    if artifact_type == "table" and not (table.get("markdown") or table.get("html") or structured_markdown):
        reasons.append("table result has no structured table content")
    key_facts = data.get("key_facts") if isinstance(data.get("key_facts"), list) else []
    summary = str(data.get("summary") or "").strip()
    if not summary and not key_facts:
        reasons.append("summary and key_facts are empty")
    should_index = explicit_should_index and not reasons
    existing_reason = str(data.get("low_confidence_reason") or "").strip()
    reason_parts = [existing_reason] if existing_reason else []
    reason_parts.extend(reasons)
    reason = "; ".join(_unique_nonempty_terms(reason_parts))
    continuation = _normalize_continuation(data.get("continuation"))
    is_partial = bool(data.get("is_partial", False))
    if continuation["role"] != "single" and continuation["confidence"] >= 0.50:
        is_partial = True
    return VisualAnalysisResult(
        artifact_type=artifact_type,
        title=str(data.get("title") or ""),
        caption=str(data.get("caption") or candidate.caption or ""),
        page=int(data.get("page") or candidate.page or 0),
        summary=summary,
        structured_markdown=structured_markdown,
        key_facts=key_facts,
        table=table,
        signals=data.get("signals") if isinstance(data.get("signals"), list) else [],
        state_machine=data.get("state_machine") if isinstance(data.get("state_machine"), dict) else {},
        chart=data.get("chart") if isinstance(data.get("chart"), dict) else {},
        uncertain_fields=data.get("uncertain_fields") if isinstance(data.get("uncertain_fields"), list) else [],
        readability=str(data.get("readability") or "unknown"),
        confidence=normalized_confidence,
        processing=data.get("processing") if isinstance(data.get("processing"), dict) else {},
        is_partial=is_partial,
        continuation=continuation,
        should_index=should_index,
        low_confidence_reason=reason,
    )


def validate_visual_group_analysis_json(
    raw: Any,
    group: Dict[str, Any],
    members: List[Dict[str, Any]],
    visual_config: Dict[str, Any],
) -> Dict[str, Any]:
    if isinstance(raw, dict):
        data = dict(raw)
    else:
        text = str(raw or "").strip()
        fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", text, flags=re.DOTALL | re.IGNORECASE)
        if fence:
            text = fence.group(1).strip()
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"visual group analysis response is not valid JSON: {exc}") from exc

    raw_has_source_pages = "source_pages" in data
    raw_source_pages = data.get("source_pages") if raw_has_source_pages else None
    model_source_pages = _normalize_source_pages(raw_source_pages) if raw_has_source_pages else []
    group_source_pages = _normalize_source_pages(group.get("source_pages"))
    member_pages = _normalize_source_pages([member.get("page") for member in members])
    source_pages = _normalize_source_pages([*model_source_pages, *group_source_pages, *member_pages])
    parts = data.get("parts") if isinstance(data.get("parts"), list) else []
    confidence = data.get("confidence") if isinstance(data.get("confidence"), dict) else {}
    normalized_confidence = {
        "ocr": _clamp01(confidence.get("ocr", 0)),
        "structure": _clamp01(confidence.get("structure", 0)),
        "semantic": _clamp01(confidence.get("semantic", 0)),
        "continuation": _clamp01(confidence.get("continuation", group.get("confidence", 0))),
        "overall": _clamp01(confidence.get("overall", 0)),
    }
    data["confidence"] = normalized_confidence
    is_multipage = (
        bool(data.get("is_multipage"))
        or len(model_source_pages) >= 2
        or len(group_source_pages) >= 2
        or len(member_pages) >= 2
        or len(source_pages) >= 2
    )
    data["is_multipage"] = is_multipage
    data["source_pages"] = source_pages
    data["parts"] = parts
    merged_table = data.get("merged_table") if isinstance(data.get("merged_table"), dict) else {}
    data["merged_table"] = merged_table
    hard_reasons: List[str] = []
    threshold_reasons: List[str] = []
    metadata_reasons: List[str] = []
    if normalized_confidence["continuation"] < 0.70:
        hard_reasons.append("continuation confidence below 0.70")
    if raw_has_source_pages and not model_source_pages:
        hard_reasons.append("source_pages is empty")
    elif not raw_has_source_pages and source_pages:
        metadata_reasons.append("source_pages missing in model output; filled from group metadata")
    if not source_pages:
        hard_reasons.append("source_pages is empty")
    if is_multipage and len(parts) < 2:
        hard_reasons.append("multipage result has fewer than 2 parts")
    if merged_table.get("rows") and not merged_table.get("headers"):
        hard_reasons.append("merged_table rows require headers")
    explicit_should_index = bool(data.get("should_index", False))
    if not explicit_should_index:
        hard_reasons.append("explicit model should_index=false")
    unreliable_member_reasons = _unreliable_group_member_reasons(members)
    hard_reasons.extend(unreliable_member_reasons)
    critical_low = bool(unreliable_member_reasons) or any(
        float(member.get("analysis_confidence") or member.get("confidence") or 0) < 0.70 for member in members
    )
    if critical_low and normalized_confidence["overall"] > 0.75:
        normalized_confidence["overall"] = 0.75
        threshold_reasons.append("critical part low confidence caps overall confidence")
    thresholds = {
        "overall": float(visual_config.get("min_confidence", 0.78)),
        "ocr": float(visual_config.get("min_ocr_confidence", 0.70)),
        "structure": float(visual_config.get("min_structure_confidence", 0.75)),
        "semantic": float(visual_config.get("min_semantic_confidence", 0.75)),
    }
    for key, threshold in thresholds.items():
        if normalized_confidence[key] < threshold:
            threshold_reasons.append(f"{key} confidence {normalized_confidence[key]:.2f} below {threshold:.2f}")
    should_index = explicit_should_index
    if hard_reasons or threshold_reasons:
        should_index = False
    data["should_index"] = should_index
    reasons = [*hard_reasons, *threshold_reasons, *metadata_reasons]
    if reasons:
        existing_reason = str(data.get("low_confidence_reason") or "").strip()
        reason_parts = [existing_reason] if existing_reason else []
        reason_parts.extend(reasons)
        data["low_confidence_reason"] = "; ".join(_unique_nonempty_terms(reason_parts))
    return data


def _normalize_source_pages(values: Any) -> List[int]:
    if values is None:
        return []
    if not isinstance(values, list):
        values = [values]
    pages: set[int] = set()
    for value in values:
        try:
            page = int(value)
        except (TypeError, ValueError):
            continue
        if page > 0:
            pages.add(page)
    return sorted(pages)


def _unreliable_group_member_reasons(members: List[Dict[str, Any]]) -> List[str]:
    reasons: List[str] = []
    for member in members:
        status = str(member.get("analysis_status") or "").strip()
        if status not in {"failed", "low_confidence"}:
            continue
        result_json = member.get("result_json") if isinstance(member.get("result_json"), dict) else {}
        has_fallback = bool(
            result_json.get("summary")
            or result_json.get("structured_markdown")
            or result_json.get("key_facts")
            or (isinstance(result_json.get("table"), dict) and (result_json["table"].get("markdown") or result_json["table"].get("rows")))
        )
        confidence = float(
            (result_json.get("confidence") or {}).get("overall")
            or member.get("analysis_confidence")
            or member.get("confidence")
            or 0
        )
        if not has_fallback or confidence < 0.70:
            artifact_id = member.get("artifact_id") or "unknown"
            reasons.append(f"member {artifact_id} analysis_status={status} lacks reliable fallback")
    return reasons


def merge_visual_group_from_member_results(group: Dict[str, Any], members: List[Dict[str, Any]]) -> Dict[str, Any]:
    ordered = sorted(members, key=lambda item: int(item.get("part_index") or 0))
    source_pages = sorted(
        {
            int(page)
            for page in [
                *(group.get("source_pages") or []),
                *[member.get("page") for member in ordered if member.get("page")],
            ]
            if page
        }
    )
    summaries: List[str] = []
    key_facts: List[Dict[str, Any]] = []
    headers: List[Any] = []
    rows: List[Any] = []
    row_page_map: List[Dict[str, int]] = []
    continuation_evidence = list((group.get("result_json") or {}).get("continuation_evidence") or [])
    confidence_values: List[float] = []
    parts: List[Dict[str, Any]] = []
    artifact_type = group.get("group_type") or "unknown"
    for member in ordered:
        result = member.get("result_json") if isinstance(member.get("result_json"), dict) else {}
        confidence = float((result.get("confidence") or {}).get("overall") or member.get("analysis_confidence") or 0)
        confidence_values.append(confidence)
        summary = str(result.get("summary") or "").strip()
        if summary:
            summaries.append(f"Page {member.get('page')}: {summary}")
        parts.append(
            {
                "page": int(member.get("page") or 0),
                "artifact_id": member.get("artifact_id") or "",
                "role": member.get("role") or "",
                "summary": summary,
                "confidence": confidence,
            }
        )
        for fact in result.get("key_facts") or []:
            if isinstance(fact, dict):
                key_facts.append(dict(fact))
        table = result.get("table") if isinstance(result.get("table"), dict) else {}
        if table.get("headers") and not headers:
            headers = list(table.get("headers") or [])
        for row in table.get("rows") or []:
            row_page_map.append({"row_index": len(rows), "page": int(member.get("page") or 0)})
            rows.append(row)
        continuation = result.get("continuation") if isinstance(result.get("continuation"), dict) else {}
        for item in continuation.get("evidence") or []:
            continuation_evidence.append(str(item))
        if result.get("artifact_type") and artifact_type in ("unknown", "figure"):
            artifact_type = str(result.get("artifact_type"))

    markdown = _markdown_table(headers, rows)
    continuation_confidence = float(group.get("confidence") or 0)
    overall = min(confidence_values) if confidence_values else 0.0
    if continuation_confidence:
        overall = min(overall or continuation_confidence, continuation_confidence)
    return {
        "artifact_type": artifact_type,
        "title": group.get("title") or group.get("caption") or "",
        "caption": group.get("caption") or "",
        "is_multipage": len(source_pages) >= 2,
        "source_pages": source_pages,
        "summary": "\n".join(summaries).strip(),
        "structured_markdown": markdown,
        "key_facts": key_facts[:80],
        "parts": parts,
        "merged_table": {"headers": headers, "rows": rows, "markdown": markdown, "html": "", "row_page_map": row_page_map},
        "continuation_evidence": _unique_nonempty_terms(continuation_evidence),
        "uncertain_continuations": [],
        "confidence": {
            "ocr": min(confidence_values) if confidence_values else 0.0,
            "structure": min(confidence_values) if confidence_values else 0.0,
            "semantic": min(confidence_values) if confidence_values else 0.0,
            "continuation": continuation_confidence,
            "overall": overall,
        },
        "should_index": True,
    }


def visual_result_to_chunks(
    candidate: VisualArtifactCandidate,
    result: VisualAnalysisResult,
    document: KnowledgeDocument,
    visual_config: Dict[str, Any],
    *,
    analysis_backend: str = "",
    analysis_model: str = "",
) -> Tuple[List[KnowledgeChunk], List[SourceSpan]]:
    model = str(analysis_model or visual_config.get("model") or "gpt-5.5")
    prompt_version = str(visual_config.get("prompt_version") or "visual-v1")
    pipeline_version = str(candidate.pipeline_version or visual_config.get("pipeline_version") or "visual-pipeline-v1")
    section_path = "/".join(candidate.section_path) if candidate.section_path else ""
    processing_metadata = {
        key: result.processing.get(key)
        for key in ("tiled", "tile_count", "high_res_retry", "high_res_page_render_dpi", "max_image_long_edge")
        if key in (result.processing or {})
    }
    metadata = {
        **processing_metadata,
        "visual_artifact_id": candidate.id,
        "visual_artifact_type": result.artifact_type,
        "visual_confidence": result.confidence.get("overall", 0.0),
        "visual_scope": "page",
        "visual_group_id": getattr(candidate, "group_id", ""),
        "retrievable": True,
        "page": candidate.page,
        "bbox": candidate.bbox,
        "caption": result.caption or candidate.caption,
        "prompt_version": prompt_version,
        "pipeline_version": pipeline_version,
        "analysis_model": model,
        "analysis_backend": analysis_backend or normalize_visual_analysis_backend(visual_config.get("analysis_backend")),
        "source": "visual_analysis",
    }
    texts = _chunk_texts(candidate, result, document)
    chunks: List[KnowledgeChunk] = []
    spans: List[SourceSpan] = []
    for kind, text in texts:
        if not text.strip():
            continue
        span_id = stable_visual_span_id(document.id, document.version_id, candidate.id, f"{kind}:{text[:500]}")
        chunk_id = stable_visual_chunk_id(document.id, document.version_id, candidate.id, kind, text)
        excerpt = f"{result.caption or candidate.caption}\n{result.summary}"[:300]
        span = SourceSpan(
            id=span_id,
            document_id=document.id,
            version_id=document.version_id,
            source_file=document.source_path,
            page_start=candidate.page,
            page_end=candidate.page,
            section_path=section_path,
            paragraph_index_start=0,
            paragraph_index_end=0,
            char_start=0,
            char_end=len(excerpt),
            bbox=candidate.bbox,
            text_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            text=excerpt or text[:300],
        )
        spans.append(span)
        chunks.append(
            KnowledgeChunk(
                id=chunk_id,
                document_id=document.id,
                ordinal=0,
                page_start=candidate.page,
                page_end=candidate.page,
                text=text,
                kb_id=document.kb_id or candidate.kb_id or "kb_default",
                version_id=document.version_id,
                section_path=section_path,
                clause_title=result.title or candidate.label or result.artifact_type,
                source_span_ids=[span_id],
                metadata=dict(metadata),
            )
        )
    return chunks, spans


def visual_group_result_to_chunks(
    group: Dict[str, Any],
    members: List[Dict[str, Any]],
    result: Dict[str, Any],
    document: KnowledgeDocument,
    visual_config: Dict[str, Any],
    *,
    analysis_backend: str = "",
    analysis_model: str = "",
) -> Tuple[List[KnowledgeChunk], List[SourceSpan]]:
    model = str(analysis_model or visual_config.get("model") or "gpt-5.5")
    prompt_version = str(visual_config.get("group_prompt_version") or "visual-group-v1")
    source_pages = sorted(
        {
            int(page)
            for page in [
                *(result.get("source_pages") or []),
                *(group.get("source_pages") or []),
                *[member.get("page") for member in members if member.get("page")],
            ]
            if page
        }
    )
    result = {**result, "source_pages": source_pages}
    page_start = min(source_pages) if source_pages else 0
    page_end = max(source_pages) if source_pages else 0
    artifact_ids = [member.get("artifact_id") for member in members if member.get("artifact_id")]
    bboxes = [
        {"page": int(member.get("page") or 0), "bbox": member.get("bbox") or {}}
        for member in members
        if member.get("bbox")
    ]
    metadata = {
        "source": "visual_analysis",
        "visual_scope": "group",
        "visual_group_id": group.get("id") or "",
        "visual_artifact_ids": artifact_ids,
        "visual_artifact_type": result.get("artifact_type") or group.get("group_type") or "unknown",
        "visual_confidence": (result.get("confidence") or {}).get("overall", 0.0),
        "visual_continuation_confidence": (result.get("confidence") or {}).get("continuation", 0.0),
        "source_pages": source_pages,
        "page_start": page_start,
        "page_end": page_end,
        "caption": result.get("caption") or group.get("caption") or "",
        "analysis_model": model,
        "analysis_backend": analysis_backend or normalize_visual_analysis_backend(visual_config.get("analysis_backend")),
        "prompt_version": prompt_version,
        "pipeline_version": str(visual_config.get("pipeline_version") or "visual-pipeline-v1"),
    }
    texts = _group_chunk_texts(group, members, result, document)
    chunks: List[KnowledgeChunk] = []
    spans: List[SourceSpan] = []
    section_path = ""
    bbox_payload = {"pages": bboxes}
    for kind, text, extra_metadata in texts:
        if not text.strip():
            continue
        span_id = stable_visual_group_span_id(document.id, document.version_id, group.get("id") or "", f"{kind}:{text[:500]}")
        chunk_id = stable_visual_group_chunk_id(document.id, document.version_id, group.get("id") or "", kind, text)
        span = SourceSpan(
            id=span_id,
            document_id=document.id,
            version_id=document.version_id,
            source_file=document.source_path,
            page_start=page_start,
            page_end=page_end,
            section_path=section_path,
            paragraph_index_start=0,
            paragraph_index_end=0,
            char_start=0,
            char_end=min(len(text), 500),
            bbox=bbox_payload,
            text_hash="sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest(),
            text=text[:500],
        )
        spans.append(span)
        chunks.append(
            KnowledgeChunk(
                id=chunk_id,
                document_id=document.id,
                ordinal=0,
                page_start=page_start,
                page_end=page_end,
                text=text,
                kb_id=document.kb_id or group.get("kb_id") or "kb_default",
                version_id=document.version_id,
                section_path=section_path,
                clause_title=result.get("title") or group.get("title") or group.get("group_type") or "visual group",
                source_span_ids=[span_id],
                metadata={**metadata, **(extra_metadata or {})},
            )
        )
    return chunks, spans


def image_file_to_data_url(path: str, max_long_edge: int = 1800) -> str:
    image_path = Path(path)
    if not image_path.is_file():
        raise FileNotFoundError(f"visual artifact image not found: {image_path}")
    payload_path = image_path
    temp_path: Path | None = None
    if max_long_edge > 0:
        try:
            from PIL import Image

            with Image.open(image_path) as image:
                long_edge = max(image.size)
                if long_edge > max_long_edge:
                    scale = max_long_edge / long_edge
                    new_size = (max(1, int(image.width * scale)), max(1, int(image.height * scale)))
                    resized = image.resize(new_size)
                    temp_path = image_path.with_suffix(".visual_resized.png")
                    resized.save(temp_path, format="PNG")
                    payload_path = temp_path
        except Exception:
            payload_path = image_path
    mime_type = mimetypes.guess_type(payload_path.name)[0] or "image/png"
    try:
        encoded = base64.b64encode(payload_path.read_bytes()).decode("ascii")
    finally:
        if temp_path and temp_path.exists():
            temp_path.unlink()
    return f"data:{mime_type};base64,{encoded}"


def _chunk_texts(candidate: VisualArtifactCandidate, result: VisualAnalysisResult, document: KnowledgeDocument) -> List[Tuple[str, str]]:
    facts = []
    for fact in result.key_facts[:20]:
        if isinstance(fact, dict):
            confidence = fact.get("confidence", "")
            suffix = f" (confidence={confidence})" if confidence != "" else ""
            facts.append(f"- {fact.get('fact', '')}{suffix}".strip())
        else:
            facts.append(f"- {fact}")
    caption = result.caption or candidate.caption
    title = result.title or caption or candidate.label
    overall = result.confidence.get("overall", 0.0)
    search_terms = _visual_search_terms(candidate, result)
    summary = "\n".join(
        [
            "[视觉图表]",
            f"Document: {document.title}",
            f"Page: {candidate.page}",
            f"Section path: {' / '.join(candidate.section_path)}",
            f"Type: {result.artifact_type}",
            f"Artifact type aliases: {_artifact_aliases(result.artifact_type)}",
            f"Title/Caption: {title}",
            f"Label: {candidate.label}",
            f"Summary: {result.summary}",
            f"Search terms: {', '.join(search_terms)}",
            "Key facts:",
            *facts,
            f"Confidence: {overall}",
        ]
    )
    texts: List[Tuple[str, str]] = [("summary", summary)]
    table_markdown = str(result.table.get("markdown") or result.table.get("html") or "")
    if result.artifact_type == "table" and (table_markdown or result.structured_markdown):
        texts.append(("table", "\n".join(["[视觉表格]", f"Page: {candidate.page}", table_markdown or result.structured_markdown]).strip()))
    details = _detail_text(result)
    if details:
        texts.append(("detail", "\n".join(["[视觉图表详情]", f"Page: {candidate.page}", details]).strip()))
    return texts[:3]


def _group_chunk_texts(
    group: Dict[str, Any],
    members: List[Dict[str, Any]],
    result: Dict[str, Any],
    document: KnowledgeDocument,
) -> List[Tuple[str, str, Dict[str, Any]]]:
    source_pages = [int(page) for page in (result.get("source_pages") or group.get("source_pages") or []) if page]
    page_range = _page_range_text(source_pages)
    confidence = result.get("confidence") if isinstance(result.get("confidence"), dict) else {}
    facts = []
    for fact in (result.get("key_facts") or [])[:30]:
        if isinstance(fact, dict):
            facts.append(f"- {fact.get('fact', '')} (confidence={fact.get('confidence', '')})".strip())
        else:
            facts.append(f"- {fact}")
    evidence = [str(item) for item in (result.get("continuation_evidence") or [])[:20]]
    summary = "\n".join(
        [
            "[视觉图表-多页]",
            f"Document: {document.title}",
            f"类型: {result.get('artifact_type') or group.get('group_type') or 'unknown'}",
            f"页码范围: {page_range}",
            f"source_pages: {', '.join(str(page) for page in source_pages)}",
            f"caption/title: {result.get('caption') or result.get('title') or group.get('caption') or group.get('title') or ''}",
            "Continuation evidence:",
            *[f"- {item}" for item in evidence],
            f"Summary: {result.get('summary') or ''}",
            "Key facts:",
            *facts,
            f"confidence: {confidence.get('overall', 0.0)}",
            f"continuation_confidence: {confidence.get('continuation', 0.0)}",
        ]
    )
    texts: List[Tuple[str, str, Dict[str, Any]]] = [("summary", summary, {})]
    merged_table = result.get("merged_table") if isinstance(result.get("merged_table"), dict) else {}
    table_markdown = str(merged_table.get("markdown") or "")
    if (result.get("artifact_type") == "table" or group.get("group_type") == "table") and table_markdown:
        texts.extend(_split_group_table_chunks(table_markdown, merged_table, source_pages))
    detail_lines = ["[视觉图表-多页详情]", f"Document: {document.title}", f"Pages: {page_range}"]
    for part in result.get("parts") or []:
        if not isinstance(part, dict):
            continue
        detail_lines.append(
            f"- Page {part.get('page')} / {part.get('role')}: {part.get('summary', '')} "
            f"(confidence={part.get('confidence', 0.0)})"
        )
    if len(detail_lines) > 3:
        texts.append(("detail", "\n".join(detail_lines), {}))
    return texts[:8]


def _split_group_table_chunks(
    table_markdown: str,
    merged_table: Dict[str, Any],
    source_pages: List[int],
) -> List[Tuple[str, str, Dict[str, Any]]]:
    rows = merged_table.get("rows") or []
    headers = merged_table.get("headers") or []
    row_page_map = merged_table.get("row_page_map") or []
    if not rows or len(table_markdown) <= 5000:
        return [
            (
                "table",
                "\n".join(["[视觉表格-多页]", f"Pages: {_page_range_text(source_pages)}", table_markdown]).strip(),
                {"row_range": [0, max(0, len(rows) - 1)], "page_range": [min(source_pages or [0]), max(source_pages or [0])]},
            )
        ]
    chunks: List[Tuple[str, str, Dict[str, Any]]] = []
    chunk_size = 40
    for start in range(0, len(rows), chunk_size):
        end = min(len(rows), start + chunk_size)
        chunk_rows = rows[start:end]
        pages = [
            int(item.get("page") or 0)
            for item in row_page_map
            if start <= int(item.get("row_index") or 0) < end and item.get("page")
        ]
        markdown = _markdown_table(headers, chunk_rows)
        chunks.append(
            (
                f"table_rows_{start}_{end - 1}",
                "\n".join(["[视觉表格-多页]", f"Rows: {start}-{end - 1}", f"Pages: {_page_range_text(pages or source_pages)}", markdown]).strip(),
                {"row_range": [start, end - 1], "page_range": [min(pages or source_pages or [0]), max(pages or source_pages or [0])]},
            )
        )
    return chunks


def _group_member_prompt_payload(member: Dict[str, Any], visual_config: Dict[str, Any]) -> Dict[str, Any]:
    result = member.get("result_json") if isinstance(member.get("result_json"), dict) else {}
    context_limit = int(visual_config.get("group_member_context_chars", 1000) or 1000)
    result_limit = int(visual_config.get("group_member_result_chars", 6000) or 6000)
    return {
        "artifact_id": member.get("artifact_id") or "",
        "page": int(member.get("page") or 0),
        "part_index": int(member.get("part_index") or 0),
        "role": member.get("role") or "",
        "caption": member.get("caption") or "",
        "label": member.get("label") or "",
        "bbox": member.get("bbox") or {},
        "analysis_status": member.get("analysis_status") or "",
        "analysis_confidence": member.get("analysis_confidence") or member.get("confidence") or 0,
        "context_before": str(member.get("context_before") or "")[:context_limit],
        "context_after": str(member.get("context_after") or "")[:context_limit],
        "page_text": str(member.get("page_text") or "")[:context_limit],
        "page_level_result": _truncate_prompt_value(result, result_limit),
    }


def _truncate_prompt_value(value: Any, limit: int) -> Any:
    text = json.dumps(value, ensure_ascii=False)
    if len(text) <= limit:
        return value
    return {"truncated_json": text[:limit], "truncated": True}


def _chat_completion_content(response: Any) -> str:
    if isinstance(response, dict):
        choices = response.get("choices") or []
        if choices and isinstance(choices[0], dict):
            message = choices[0].get("message") or {}
            if isinstance(message, dict):
                content = message.get("content")
                if content:
                    return str(content)
            delta = choices[0].get("delta") or {}
            if isinstance(delta, dict) and delta.get("content"):
                return str(delta.get("content"))
        if response.get("content"):
            return str(response.get("content"))
    return str(response or "")


def _markdown_table(headers: List[Any], rows: List[Any]) -> str:
    if not headers or not rows:
        return ""
    header_text = [str(header) for header in headers]
    lines = [
        "| " + " | ".join(header_text) + " |",
        "| " + " | ".join("---" for _ in header_text) + " |",
    ]
    for row in rows:
        if isinstance(row, dict):
            values = [str(row.get(header, "")) for header in header_text]
        elif isinstance(row, list):
            values = [str(value) for value in row]
        else:
            values = [str(row)]
        if len(values) < len(header_text):
            values.extend("" for _ in range(len(header_text) - len(values)))
        lines.append("| " + " | ".join(values[: len(header_text)]) + " |")
    return "\n".join(lines)


def _page_range_text(source_pages: List[int]) -> str:
    if not source_pages:
        return ""
    values = sorted(set(source_pages))
    if len(values) == 1:
        return f"Page {values[0]}"
    return f"Page {values[0]}-{values[-1]}"


def _detail_text(result: VisualAnalysisResult) -> str:
    lines: List[str] = []
    if result.structured_markdown and result.artifact_type != "table":
        lines.extend(["Structured content:", result.structured_markdown])
    if result.signals:
        lines.append("Signals:")
        for signal in result.signals[:50]:
            lines.append(json.dumps(signal, ensure_ascii=False))
    headers = (result.table or {}).get("headers") or []
    if headers:
        lines.append("Table headers:")
        lines.append(", ".join(str(header) for header in headers))
    rows = (result.table or {}).get("rows") or []
    if rows:
        lines.append("Table rows excerpt:")
        for row in rows[:20]:
            lines.append(json.dumps(row, ensure_ascii=False))
    states = (result.state_machine or {}).get("states") or []
    if states:
        lines.append("State names:")
        lines.append(", ".join(str(state) for state in states[:80]))
    transitions = (result.state_machine or {}).get("transitions") or []
    if transitions:
        lines.append("State machine transitions:")
        for transition in transitions[:80]:
            lines.append(json.dumps(transition, ensure_ascii=False))
    observations = (result.chart or {}).get("observations") or []
    if observations:
        lines.append("Chart observations:")
        for observation in observations[:50]:
            lines.append(json.dumps(observation, ensure_ascii=False))
    return "\n".join(lines).strip()


def _artifact_aliases(artifact_type: str) -> str:
    base = {
        "table": ["table", "表格", "grid", "matrix"],
        "figure": ["figure", "fig", "diagram", "图", "图表"],
        "chart": ["chart", "plot", "curve", "图表"],
        "timing_diagram": ["timing", "timing diagram", "waveform", "时序", "时序图"],
        "waveform": ["waveform", "timing", "时序", "波形"],
        "state_machine": ["state machine", "fsm", "state transition", "状态机", "状态转换"],
        "bitfield": ["bit field", "bitfield", "register field", "位域", "字段"],
        "flowchart": ["flowchart", "flow diagram", "流程图"],
        "image": ["image", "figure", "diagram", "图片"],
    }
    aliases = base.get(str(artifact_type or "").lower(), ["figure", "diagram", "图表"])
    return ", ".join(aliases)


def _visual_search_terms(candidate: VisualArtifactCandidate, result: VisualAnalysisResult) -> List[str]:
    values: List[str] = [
        result.artifact_type,
        _artifact_aliases(result.artifact_type),
        result.title,
        result.caption or candidate.caption,
        candidate.label,
        str(candidate.page),
        " / ".join(candidate.section_path),
    ]
    for fact in result.key_facts[:50]:
        if isinstance(fact, dict):
            values.append(str(fact.get("fact") or ""))
        else:
            values.append(str(fact))
    for signal in result.signals[:80]:
        if isinstance(signal, dict):
            values.extend(str(signal.get(key) or "") for key in ("name", "direction", "width", "meaning"))
    table = result.table or {}
    for header in table.get("headers") or []:
        values.append(str(header))
    for row in table.get("rows") or []:
        if isinstance(row, dict):
            values.extend(str(value) for value in row.values())
        elif isinstance(row, list):
            values.extend(str(value) for value in row)
    state_machine = result.state_machine or {}
    values.extend(str(state) for state in state_machine.get("states") or [])
    for transition in state_machine.get("transitions") or []:
        if isinstance(transition, dict):
            values.extend(str(transition.get(key) or "") for key in ("from", "to", "condition", "action"))
    chart = result.chart or {}
    for key in ("axes", "series", "observations"):
        for item in chart.get(key) or []:
            values.append(json.dumps(item, ensure_ascii=False) if isinstance(item, (dict, list)) else str(item))
    return _unique_nonempty_terms(values)


def _unique_nonempty_terms(values: List[str]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    return result[:160]


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))


def _normalize_continuation(value: Any) -> Dict[str, Any]:
    payload = value if isinstance(value, dict) else {}
    role = str(payload.get("role") or "single").strip().lower()
    if role not in {"single", "first", "middle", "last", "unknown"}:
        role = "unknown"
    evidence = payload.get("evidence") if isinstance(payload.get("evidence"), list) else []
    return {
        "role": role,
        "belongs_to_same_artifact": bool(payload.get("belongs_to_same_artifact", False)),
        "evidence": _unique_nonempty_terms([str(item) for item in evidence]),
        "confidence": _clamp01(payload.get("confidence", 0)),
    }
