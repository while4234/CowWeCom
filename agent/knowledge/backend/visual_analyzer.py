"""Vision-model analysis and chunk conversion for visual knowledge artifacts."""

from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from .models import KnowledgeChunk, KnowledgeDocument, SourceSpan, VisualAnalysisResult, VisualArtifactCandidate
from .storage import stable_visual_chunk_id, stable_visual_span_id


SYSTEM_PROMPT = (
    "你是协议文档视觉元素解析器。你的任务是把协议中的表格、图、时序图、状态机、"
    "位域图、流程图、截图、嵌入图片解析为可用于 RAG 检索的高质量结构化内容。"
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
    "should_index": True,
    "low_confidence_reason": "",
}


class VisualAnalyzer:
    """Analyze one visual artifact via the configured OpenAI-compatible vision path."""

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
    return "Analyze this protocol visual artifact. Return strict JSON only.\n\n" + json.dumps(payload, ensure_ascii=False, indent=2)


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
    should_index = bool(data.get("should_index", False))
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
    if not bool(visual_config.get("index_low_confidence", False)) and reasons:
        should_index = False
    reason = str(data.get("low_confidence_reason") or "; ".join(reasons))
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
        should_index=should_index,
        low_confidence_reason=reason,
    )


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
    section_path = "/".join(candidate.section_path) if candidate.section_path else ""
    metadata = {
        "visual_artifact_id": candidate.id,
        "visual_artifact_type": result.artifact_type,
        "visual_confidence": result.confidence.get("overall", 0.0),
        "retrievable": True,
        "page": candidate.page,
        "bbox": candidate.bbox,
        "caption": result.caption or candidate.caption,
        "prompt_version": prompt_version,
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
    summary = "\n".join(
        [
            "[视觉图表]",
            f"Document: {document.title}",
            f"Page: {candidate.page}",
            f"Type: {result.artifact_type}",
            f"Title/Caption: {title}",
            f"Summary: {result.summary}",
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


def _detail_text(result: VisualAnalysisResult) -> str:
    lines: List[str] = []
    if result.structured_markdown and result.artifact_type != "table":
        lines.extend(["Structured content:", result.structured_markdown])
    if result.signals:
        lines.append("Signals:")
        for signal in result.signals[:50]:
            lines.append(json.dumps(signal, ensure_ascii=False))
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


def _clamp01(value: Any) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    return max(0.0, min(1.0, number))
