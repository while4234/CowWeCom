"""Visual artifact extraction for local document knowledge backend PDFs."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from common.log import logger

from .models import ExtractedDocument, KnowledgeDocument, VisualArtifactCandidate
from .storage import stable_visual_artifact_id
from .visual_grouping import bbox_iou


DEFAULT_VISUAL_PIPELINE_VERSION = "visual-pipeline-v2"
_CAPTION_NUMBER_PATTERN = r"\d+(?:[-.]\d+)*(?![-.]\d)"
_CAPTION_LABEL_PATTERN = rf"(?:(?:Figure|Fig\.?|Table)\s+{_CAPTION_NUMBER_PATTERN}|[图表]\s*{_CAPTION_NUMBER_PATTERN})"
_CAPTION_PREFIX = rf"(?P<label>{_CAPTION_LABEL_PATTERN})"
STRICT_CAPTION_LABEL_RE = re.compile(rf"^\s*{_CAPTION_PREFIX}\s*[.:：]\s*$", re.IGNORECASE)
STRICT_CAPTION_BLOCK_RE = re.compile(
    rf"^\s*{_CAPTION_PREFIX}(?:\s*[.:：]\s*|\s+)(?P<title>\S.*)$",
    re.IGNORECASE,
)
STRICT_CAPTION_RE = STRICT_CAPTION_BLOCK_RE
VISUAL_KEYWORD_RE = re.compile(
    r"\b(?:timing|waveform|state\s*machine|bit\s*field|diagram|chart)\b|时序|状态机|流程图|位域",
    re.IGNORECASE,
)
FORMULA_CONTEXT_RE = re.compile(
    r"\b(?:equation|formula|loss|polynomial|crc|vtf|burst\s+address|transfer\s+function|ceil|floor|log)\b|公式|方程",
    re.IGNORECASE,
)
TABLE_CONTINUATION_RE = re.compile(r"\b(?:continued|cont['’]?d)\b|续表|接上页|上页续|下页继续", re.IGNORECASE)
TABLE_HEADER_RE = re.compile(
    r"\b(?:signal|direction|description|name|type|bit|field|width|value|encoding|meaning|parameter|module|layer)\b",
    re.IGNORECASE,
)
CAPTION_RE = STRICT_CAPTION_RE
_REFERENCE_CAPTION_VERBS = {
    "show",
    "shows",
    "shown",
    "illustrate",
    "illustrates",
    "illustrated",
    "demonstrate",
    "demonstrates",
    "demonstrated",
    "summarize",
    "summarizes",
    "summarized",
    "give",
    "gives",
    "given",
    "represent",
    "represents",
    "represented",
    "describe",
    "describes",
    "described",
    "list",
    "lists",
    "listed",
    "provide",
    "provides",
    "provided",
    "explain",
    "explains",
    "explained",
    "depict",
    "depicts",
    "depicted",
}
_BODY_REFERENCE_VERBS = {
    *_REFERENCE_CAPTION_VERBS,
    "define",
    "defines",
    "defined",
    "indicate",
    "indicates",
    "indicated",
    "present",
    "presents",
    "presented",
    "see",
    "specify",
    "specifies",
    "specified",
    "use",
    "uses",
    "used",
}
_REFERENCE_ITEM_PATTERN = rf"(?:{_CAPTION_LABEL_PATTERN})(?:\s*\([^)\n]{{1,80}}\))?"
_REFERENCE_CLUSTER_RE = re.compile(
    rf"^\s*(?P<cluster>{_REFERENCE_ITEM_PATTERN}"
    rf"(?:(?:\s*,\s*(?:and\s+)?|\s+and\s+|\s+to\s+){_REFERENCE_ITEM_PATTERN})*)"
    rf"(?P<tail>.*)$",
    re.IGNORECASE,
)
_BODY_REFERENCE_VERB_RE = re.compile(
    r"^(?:" + "|".join(sorted(re.escape(verb) for verb in _BODY_REFERENCE_VERBS)) + r")\b",
    re.IGNORECASE,
)
_BODY_REFERENCE_PHRASE_RE = re.compile(
    r"^(?:"
    r"on\s+pages?\b|"
    r"as\s+shown\b|"
    r"(?:is|are)\s+(?:an?\s+)?(?:example|examples|illustration|summary)\b"
    r")",
    re.IGNORECASE,
)
_REFERENCE_TITLE_START_RE = re.compile(
    rf"^(?:on\s+pages?|in\s+pages?|and\s+(?:{_CAPTION_LABEL_PATTERN})|to\s+(?:{_CAPTION_LABEL_PATTERN}))\b",
    re.IGNORECASE,
)
_REFERENCE_RANGE_OR_LIST_RE = re.compile(
    rf"\b(?:{_CAPTION_LABEL_PATTERN})\s+(?:and|to)\s+(?:{_CAPTION_LABEL_PATTERN})\b",
    re.IGNORECASE,
)
_REFERENCE_SENTENCE_VERB_RE = re.compile(
    r"\b(?:show|shows|demonstrate|demonstrates|give|gives|list|lists|describe|describes|represent|represents|"
    r"illustrate|illustrates|provide|provides|explain|explains|depict|depicts)\b",
    re.IGNORECASE,
)


def normalize_caption_text(text: str) -> str:
    """Normalize the caption lines that are safe to persist as labels."""

    lines = _first_nonempty_lines(text, limit=3)
    if not lines:
        return ""
    if STRICT_CAPTION_BLOCK_RE.match(lines[0]):
        return lines[0]
    if is_caption_label_line(lines[0]) and len(lines) >= 2:
        return "\n".join(lines[:2])
    return lines[0]


def is_caption_label_line(line: str) -> bool:
    return bool(STRICT_CAPTION_LABEL_RE.match(_normalize_caption_line(line)))


def is_strict_caption_block(text: str) -> bool:
    """Return True only for actual caption blocks, not body references."""

    lines = _first_nonempty_lines(text, limit=3)
    if not lines:
        return False
    if _looks_like_body_figure_table_reference("\n".join(lines)):
        return False
    first = lines[0]
    inline_match = STRICT_CAPTION_BLOCK_RE.match(first)
    if inline_match:
        return not _caption_line_looks_like_reference(first, inline_match.group("title"))
    if is_caption_label_line(first) and len(lines) >= 2:
        return not _caption_title_looks_like_reference(lines[1])
    return False


def _first_nonempty_lines(text: str, *, limit: int) -> List[str]:
    lines: List[str] = []
    for raw_line in str(text or "").splitlines():
        line = _normalize_caption_line(raw_line)
        if line:
            lines.append(line)
        if len(lines) >= limit:
            break
    return lines


def _normalize_caption_line(line: str) -> str:
    return re.sub(r"[ \t]+", " ", str(line or "").strip())


def _caption_line_looks_like_reference(line: str, title: str = "") -> bool:
    normalized = _normalize_caption_line(line)
    if _looks_like_body_figure_table_reference(normalized):
        return True
    if _REFERENCE_RANGE_OR_LIST_RE.search(normalized):
        return True
    return _caption_title_looks_like_reference(title)


def _looks_like_body_figure_table_reference(text: str) -> bool:
    normalized = _normalize_reference_line(text)
    if not normalized:
        return False
    match = _REFERENCE_CLUSTER_RE.match(normalized)
    if not match:
        return False
    tail = _normalize_reference_tail(match.group("tail") or "")
    if not tail:
        return False
    return bool(_BODY_REFERENCE_PHRASE_RE.match(tail) or _BODY_REFERENCE_VERB_RE.match(tail))


def _normalize_reference_line(text: str) -> str:
    normalized = _normalize_caption_line(re.sub(r"\s+", " ", str(text or "")))
    # Drop simple printed page numbers from list/table-of-figures entries before
    # judging whether the remaining text is a body reference sentence.
    return re.sub(r"(?:\.{2,}\s*)?\s+\d+(?:[-.]\d+)?\s*$", "", normalized).strip()


def _normalize_reference_tail(text: str) -> str:
    tail = _normalize_caption_line(text)
    return re.sub(r"^[.:：．]\s*", "", tail).strip()


def _caption_title_looks_like_reference(title: str) -> bool:
    text = _normalize_caption_line(title)
    if not text:
        return True
    first_word_match = re.match(r"([A-Za-z]+)", text)
    if first_word_match and first_word_match.group(1).lower() in _REFERENCE_CAPTION_VERBS:
        return True
    if _REFERENCE_TITLE_START_RE.search(text):
        return True
    if _REFERENCE_RANGE_OR_LIST_RE.search(text):
        return True
    tokens = text.split()
    if len(tokens) >= 9 and text.endswith(".") and _REFERENCE_SENTENCE_VERB_RE.search(text):
        return True
    if len(tokens) >= 8:
        one_char_tokens = sum(1 for token in tokens if len(token) == 1 and token.isalnum())
        if one_char_tokens / max(1, len(tokens)) >= 0.55:
            return True
    compact = re.sub(r"[^A-Za-z0-9_]", "", text).lower()
    signal_markers = sum(1 for marker in ("rx", "tx", "clk", "ck", "data", "vld", "sb") if marker in compact)
    return bool(len(compact) >= 16 and signal_markers >= 3)


class VisualArtifactExtractor:
    """Interface for parser-specific visual artifact candidate extraction."""

    def extract_candidates(
        self,
        document: KnowledgeDocument,
        extracted_document: ExtractedDocument,
        storage: Any,
        config: Any,
    ) -> List[VisualArtifactCandidate]:
        raise NotImplementedError


class PyMuPDFVisualArtifactExtractor(VisualArtifactExtractor):
    """Extract embedded images and caption-led page regions using PyMuPDF."""

    parser_name = "pymupdf"

    def extract_candidates(
        self,
        document: KnowledgeDocument,
        extracted_document: ExtractedDocument,
        storage: Any,
        config: Any,
    ) -> List[VisualArtifactCandidate]:
        page_count = len(extracted_document.pages) or 10**9
        candidates, _ = self.extract_candidates_for_page_range(
            document,
            extracted_document,
            storage,
            config,
            start_page=1,
            max_pages=page_count,
        )
        return candidates

    def extract_candidates_for_page_range(
        self,
        document: KnowledgeDocument,
        extracted_document: ExtractedDocument,
        storage: Any,
        config: Any,
        start_page: int,
        max_pages: int,
    ) -> Tuple[List[VisualArtifactCandidate], Dict[str, Any]]:
        source = self._source_path(document, extracted_document, config)
        start_page = max(1, int(start_page or 1))
        max_pages = max(1, int(max_pages or 1))
        end_page = start_page + max_pages - 1
        report: Dict[str, Any] = {
            "start_page": start_page,
            "end_page": end_page,
            "pages_scanned": 0,
            "candidates": 0,
            "skipped_toc_pages": 0,
        }
        if source.suffix.lower() != ".pdf" or not source.is_file():
            return [], report
        try:
            import fitz
        except ImportError:
            logger.warning("[KnowledgeBackend] PyMuPDF is not installed; visual artifact extraction skipped")
            return [], report

        visual_config = getattr(config, "visual_analysis", {}) or {}
        dpi = int(visual_config.get("page_render_dpi", 180))
        padding = int(visual_config.get("crop_padding_px", 12))
        min_area_ratio = float(visual_config.get("candidate_min_area_ratio", 0.015))
        max_image_candidates = max(0, int(visual_config.get("max_image_candidates_per_page", 3) or 0))
        pipeline_version = str(visual_config.get("pipeline_version") or DEFAULT_VISUAL_PIPELINE_VERSION)
        candidates: List[VisualArtifactCandidate] = []
        page_texts = {page.page: page.text for page in extracted_document.pages}

        with fitz.open(str(source)) as pdf:
            for page_index, page in enumerate(pdf, start=1):
                if page_index < start_page:
                    continue
                if page_index > end_page:
                    break
                report["pages_scanned"] += 1
                page_rect = page.rect
                page_area = max(1.0, float(page_rect.width * page_rect.height))
                text_blocks = self._text_blocks(page)
                page_text = page_texts.get(page_index) or page.get_text("text") or ""
                if _is_toc_or_list_page(page_text):
                    report["skipped_toc_pages"] += 1
                    continue

                table_rects = self._table_rects(page, page_rect, text_blocks, page_text)
                caption_candidates = self._caption_candidates(page_rect, text_blocks, table_rects)
                page_candidates: List[VisualArtifactCandidate] = []
                for rect, caption, artifact_type in caption_candidates:
                    if self._area_ratio(rect, page_area) < min_area_ratio:
                        continue
                    page_candidates.append(
                        self._candidate_from_rect(
                            rect,
                            document,
                            extracted_document,
                            artifact_type,
                            page_index,
                            page_text,
                            dpi,
                            padding,
                            source_path=str(source),
                            pipeline_version=pipeline_version,
                            caption=caption,
                            parser_confidence=0.85,
                        )
                    )

                for rect, parser_confidence in self._standalone_table_rects(
                    page_rect,
                    table_rects,
                    caption_candidates,
                    page_text,
                    page_texts.get(page_index - 1, ""),
                ):
                    if self._area_ratio(rect, page_area) < min_area_ratio:
                        continue
                    page_candidates.append(
                        self._candidate_from_rect(
                            rect,
                            document,
                            extracted_document,
                            "table",
                            page_index,
                            page_text,
                            dpi,
                            padding,
                            source_path=str(source),
                            pipeline_version=pipeline_version,
                            caption="",
                            parser_confidence=parser_confidence,
                        )
                    )

                formula_rect, formula_confidence = self._formula_candidate_rect(page_rect, text_blocks, page_text)
                if formula_rect is not None and self._area_ratio(formula_rect, page_area) >= min_area_ratio:
                    page_candidates.append(
                        self._candidate_from_rect(
                            formula_rect,
                            document,
                            extracted_document,
                            "formula",
                            page_index,
                            page_text,
                            dpi,
                            padding,
                            source_path=str(source),
                            pipeline_version=pipeline_version,
                            caption="",
                            parser_confidence=formula_confidence,
                        )
                    )

                image_candidates = 0
                for rect in self._image_rects(page):
                    if self._area_ratio(rect, page_area) < min_area_ratio:
                        continue
                    if caption_candidates and any(_rect_overlap_ratio(rect, caption_rect) >= 0.65 for caption_rect, _, _ in caption_candidates):
                        continue
                    if image_candidates >= max_image_candidates:
                        continue
                    page_candidates.append(
                        self._candidate_from_rect(
                            rect,
                            document,
                            extracted_document,
                            "image",
                            page_index,
                            page_text,
                            dpi,
                            padding,
                            source_path=str(source),
                            pipeline_version=pipeline_version,
                            parser_confidence=0.70,
                        )
                    )
                    image_candidates += 1

                for candidate in page_candidates:
                    candidates.append(candidate)

                if self._should_add_page_fallback(page_text, caption_candidates, page_candidates):
                    page_candidates.append(
                        self._candidate_from_rect(
                            page_rect,
                            document,
                            extracted_document,
                            "figure",
                            page_index,
                            page_text,
                            dpi,
                            padding,
                            source_path=str(source),
                            pipeline_version=pipeline_version,
                            caption="",
                            parser_confidence=0.55,
                        )
                    )
                    candidates.append(page_candidates[-1])

        deduped = self._dedupe(candidates)
        report["candidates"] = len(deduped)
        return deduped, report

    def _source_path(self, document: KnowledgeDocument, extracted_document: ExtractedDocument, config: Any) -> Path:
        source_path = document.source_path or extracted_document.source_path
        source = Path(source_path)
        if source.is_absolute():
            return source
        workspace = Path(getattr(config, "workspace_root", ".")).expanduser().resolve()
        return (workspace / source).resolve()

    def _text_blocks(self, page: Any) -> List[Dict[str, Any]]:
        blocks: List[Dict[str, Any]] = []
        for raw in page.get_text("blocks") or []:
            if len(raw) < 5:
                continue
            text = str(raw[4] or "").strip()
            if not text:
                continue
            blocks.append({"bbox": tuple(float(value) for value in raw[:4]), "text": text})
        return blocks

    def _image_rects(self, page: Any) -> Iterable[Any]:
        for image in page.get_images(full=True) or []:
            xref = image[0]
            for rect in page.get_image_rects(xref) or []:
                yield rect

    def _caption_candidates(
        self,
        page_rect: Any,
        blocks: List[Dict[str, Any]],
        table_rects: List[Any],
    ) -> List[Tuple[Any, str, str]]:
        try:
            import fitz
        except ImportError:
            return []
        candidates = []
        for index, block in enumerate(blocks):
            text = self._caption_text_from_block(blocks, index)
            if _looks_like_toc_entry(text) or not is_strict_caption_block(text):
                continue
            x0, y0, x1, y1 = block["bbox"]
            caption_rect = fitz.Rect(x0, y0, x1, y1)
            caption = normalize_caption_text(text)
            artifact_type = self._artifact_type_from_caption(caption)
            table_rect = self._best_table_rect_for_caption(caption_rect, table_rects) if artifact_type == "table" else None
            rect = _union_rects([caption_rect, table_rect]) if table_rect is not None else self._caption_visual_rect(page_rect, caption_rect)
            candidates.append((rect, caption[:300], artifact_type))
        return candidates

    def _table_rects(self, page: Any, page_rect: Any, blocks: List[Dict[str, Any]], page_text: str) -> List[Any]:
        if _is_toc_or_list_page(page_text):
            return []
        rects: List[Any] = []
        rects.extend(self._pymupdf_table_rects(page))
        rects.extend(self._dense_table_block_rects(page_rect, blocks))
        return _dedupe_rects(rects)

    def _pymupdf_table_rects(self, page: Any) -> List[Any]:
        finder = getattr(page, "find_tables", None)
        if not callable(finder):
            return []
        try:
            found = finder()
        except Exception:
            return []
        tables = getattr(found, "tables", found)
        rects = []
        try:
            iterator = list(tables or [])
        except TypeError:
            iterator = []
        for table in iterator:
            bbox = getattr(table, "bbox", None)
            if not bbox:
                continue
            try:
                import fitz

                rect = fitz.Rect(bbox)
            except Exception:
                continue
            if rect.width > 1 and rect.height > 1:
                rects.append(rect)
        return rects

    def _dense_table_block_rects(self, page_rect: Any, blocks: List[Dict[str, Any]]) -> List[Any]:
        try:
            import fitz
            from .text_sanitizer import is_large_table_like_block
        except Exception:
            return []

        rects: List[Any] = []
        tableish_blocks: List[Dict[str, Any]] = []
        for block in blocks:
            text = str(block.get("text") or "")
            if is_large_table_like_block(text) or _block_looks_table_like(text):
                x0, y0, x1, y1 = block["bbox"]
                block = {**block, "rect": fitz.Rect(x0, y0, x1, y1)}
                tableish_blocks.append(block)
                if is_large_table_like_block(text):
                    rects.append(block["rect"])
        if len(tableish_blocks) >= 5:
            rects.append(_union_rects([block["rect"] for block in tableish_blocks]))
        if _page_edge_dense_table_like(tableish_blocks, page_rect):
            rects.append(_union_rects([block["rect"] for block in tableish_blocks]))
        return [rect for rect in rects if rect is not None and rect.width > 1 and rect.height > 1]

    def _standalone_table_rects(
        self,
        page_rect: Any,
        table_rects: List[Any],
        caption_candidates: List[Tuple[Any, str, str]],
        page_text: str,
        previous_page_text: str,
    ) -> List[Tuple[Any, float]]:
        if not table_rects or _is_toc_or_list_page(page_text):
            return []
        caption_rects = [rect for rect, _caption, artifact_type in caption_candidates if artifact_type == "table"]
        candidates: List[Tuple[Any, float]] = []
        continuation_hint = _table_continuation_hint(page_text, previous_page_text)
        for rect in table_rects:
            if any(_rect_overlap_ratio(rect, caption_rect) >= 0.72 for caption_rect in caption_rects):
                continue
            edge_hint = _rect_touches_page_edge(rect, page_rect)
            if continuation_hint or edge_hint or _block_text_dense_table_like(page_text):
                confidence = 0.70 if continuation_hint or edge_hint else 0.64
                candidates.append((rect, confidence))
        return candidates

    def _best_table_rect_for_caption(self, caption_rect: Any, table_rects: List[Any]) -> Optional[Any]:
        if not table_rects:
            return None
        caption_mid = (float(caption_rect.y0) + float(caption_rect.y1)) / 2.0
        best = None
        best_distance = 10**9
        for rect in table_rects:
            distance = min(abs(float(rect.y0) - caption_mid), abs(float(rect.y1) - caption_mid))
            if distance < best_distance:
                best = rect
                best_distance = distance
        return best if best_distance <= max(float(caption_rect.height) * 12, 180.0) else None

    def _formula_candidate_rect(self, page_rect: Any, blocks: List[Dict[str, Any]], page_text: str) -> Tuple[Optional[Any], float]:
        if _is_toc_or_list_page(page_text):
            return None, 0.0
        try:
            import fitz
            from .text_sanitizer import is_formula_garble_block, is_formula_garble_line
        except Exception:
            return None, 0.0
        context_hint = bool(FORMULA_CONTEXT_RE.search(page_text or ""))
        block_rects: List[Any] = []
        for index, block in enumerate(blocks):
            text = str(block.get("text") or "")
            neighboring_context = "\n".join(
                str(blocks[pos].get("text") or "")
                for pos in range(max(0, index - 1), min(len(blocks), index + 2))
            )
            formulaish = is_formula_garble_block(f"{neighboring_context}\n{text}") or any(
                is_formula_garble_line(line, context=neighboring_context) for line in text.splitlines()
            )
            keyword_with_symbols = bool(FORMULA_CONTEXT_RE.search(neighboring_context) and re.search(r"[=+\-*/^(){}\[\]]", text))
            if formulaish or keyword_with_symbols:
                x0, y0, x1, y1 = block["bbox"]
                block_rects.append(fitz.Rect(x0, y0, x1, y1))
        if block_rects:
            return _expand_rect(_union_rects(block_rects), page_rect, y_padding=24.0), 0.60
        if context_hint and is_formula_garble_block(page_text):
            return page_rect, 0.55
        return None, 0.0

    def _caption_visual_rect(self, page_rect: Any, caption_rect: Any) -> Any:
        """Return a crop that includes the likely visual region, not only the caption."""

        try:
            import fitz
        except ImportError:
            return page_rect
        page_height = max(1.0, float(page_rect.height))
        caption_mid = (float(caption_rect.y0) + float(caption_rect.y1)) / 2.0
        normalized_y = (caption_mid - float(page_rect.y0)) / page_height
        if normalized_y >= 0.58:
            top = max(float(page_rect.y0), float(caption_rect.y0) - page_height * 0.72)
            bottom = min(float(page_rect.y1), float(caption_rect.y1) + page_height * 0.08)
        elif normalized_y <= 0.42:
            top = max(float(page_rect.y0), float(caption_rect.y0) - page_height * 0.08)
            bottom = min(float(page_rect.y1), float(caption_rect.y1) + page_height * 0.72)
        else:
            top = max(float(page_rect.y0), float(caption_rect.y0) - page_height * 0.45)
            bottom = min(float(page_rect.y1), float(caption_rect.y1) + page_height * 0.45)
        if bottom - top < page_height * 0.24:
            center = (top + bottom) / 2.0
            top = max(float(page_rect.y0), center - page_height * 0.12)
            bottom = min(float(page_rect.y1), center + page_height * 0.12)
        return fitz.Rect(page_rect.x0, top, page_rect.x1, bottom)

    def _caption_text_from_block(self, blocks: List[Dict[str, Any]], index: int) -> str:
        text = str(blocks[index].get("text") or "")
        lines = _first_nonempty_lines(text, limit=3)
        if is_caption_label_line(lines[0] if lines else "") and len(lines) < 2 and index + 1 < len(blocks):
            next_text = str(blocks[index + 1].get("text") or "")
            next_lines = _first_nonempty_lines(next_text, limit=1)
            if next_lines:
                return f"{text.rstrip()}\n{next_lines[0]}"
        return text

    def _candidate_from_rect(
        self,
        rect: Any,
        document: KnowledgeDocument,
        extracted_document: ExtractedDocument,
        artifact_type: str,
        page_number: int,
        page_text: str,
        dpi: int,
        padding: int,
        *,
        source_path: str,
        pipeline_version: str = "",
        caption: str = "",
        parser_confidence: float,
    ) -> VisualArtifactCandidate:
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PyMuPDF is required for visual artifact extraction") from exc
        scale = dpi / 72.0
        clip = fitz.Rect(rect)
        pad_points = padding / max(scale, 0.1)
        bbox = {
            "x0": round(float(clip.x0), 3),
            "y0": round(float(clip.y0), 3),
            "x1": round(float(clip.x1), 3),
            "y1": round(float(clip.y1), 3),
            "page_width": round(float(rect.parent.width), 3) if getattr(rect, "parent", None) else 0,
            "page_height": round(float(rect.parent.height), 3) if getattr(rect, "parent", None) else 0,
            "unit": "pdf_points",
        }
        context_before, context_after = self._context_around_caption(
            extracted_document,
            page_number,
            page_text,
            caption,
        )
        pipeline_version = str(pipeline_version or DEFAULT_VISUAL_PIPELINE_VERSION)
        context_hash = hashlib.sha256(
            "\n".join([pipeline_version, caption, context_before, context_after, page_text[:2000]]).encode("utf-8")
        ).hexdigest()
        image_hash = self._lazy_image_prehash(
            document.id,
            document.version_id,
            page_number,
            artifact_type,
            bbox,
            caption,
            context_hash,
        )
        artifact_id = stable_visual_artifact_id(
            document.id,
            document.version_id,
            page_number,
            image_hash,
            artifact_type,
            bbox,
        )
        return VisualArtifactCandidate(
            id=artifact_id,
            document_id=document.id,
            version_id=document.version_id,
            kb_id=document.kb_id or "kb_default",
            artifact_type=artifact_type,
            page=page_number,
            label=self._label(caption),
            caption=caption,
            bbox=bbox,
            image_path="",
            image_hash=image_hash,
            context_hash=context_hash,
            pipeline_version=pipeline_version,
            parser=self.parser_name,
            parser_confidence=parser_confidence,
            section_path=self._section_path(page_text),
            context_before=context_before,
            context_after=context_after,
            page_text=page_text[:3000],
            source_path=source_path,
            crop_dpi=dpi,
            crop_padding_px=padding,
        )

    def ensure_visual_artifact_image(
        self,
        candidate: VisualArtifactCandidate,
        config: Any,
    ) -> VisualArtifactCandidate:
        source = Path(candidate.source_path or "")
        if not source.is_file():
            document = KnowledgeDocument(
                id=candidate.document_id,
                title="",
                source_path=candidate.source_path,
                mime_type="application/pdf",
                size=0,
                content_hash="",
                status="ready",
                version_id=candidate.version_id,
            )
            source = self._source_path(document, ExtractedDocument("", candidate.source_path, "", []), config)
        try:
            import fitz
        except ImportError as exc:
            raise RuntimeError("PyMuPDF is required for visual artifact crop rendering") from exc
        output_root = Path(config.data_dir) / "visual_artifacts" / candidate.document_id / candidate.version_id
        output_root.mkdir(parents=True, exist_ok=True)
        with fitz.open(str(source)) as pdf:
            page = pdf[int(candidate.page) - 1]
            scale = int(candidate.crop_dpi or 180) / 72.0
            padding = int(candidate.crop_padding_px or 12)
            bbox = candidate.bbox or {}
            clip = fitz.Rect(
                float(bbox.get("x0", page.rect.x0)),
                float(bbox.get("y0", page.rect.y0)),
                float(bbox.get("x1", page.rect.x1)),
                float(bbox.get("y1", page.rect.y1)),
            )
            pad_points = padding / max(scale, 0.1)
            clip.x0 = max(page.rect.x0, clip.x0 - pad_points)
            clip.y0 = max(page.rect.y0, clip.y0 - pad_points)
            clip.x1 = min(page.rect.x1, clip.x1 + pad_points)
            clip.y1 = min(page.rect.y1, clip.y1 + pad_points)
            pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=clip, alpha=False)
            image_bytes = pix.tobytes("png")
        image_hash = hashlib.sha256(image_bytes).hexdigest()
        image_path = output_root / f"{candidate.id}.png"
        image_path.write_bytes(image_bytes)
        return VisualArtifactCandidate(
            **{
                **candidate.to_dict(),
                "image_path": str(image_path),
                "image_hash": image_hash,
            }
        )

    def _lazy_image_prehash(
        self,
        document_id: str,
        version_id: str,
        page: int,
        artifact_type: str,
        bbox: Dict[str, Any],
        caption: str,
        context_hash: str,
    ) -> str:
        bbox_json = str(sorted((bbox or {}).items()))
        raw = f"{document_id}|{version_id}|{page}|{artifact_type}|{bbox_json}|{caption}|{context_hash}"
        return "prehash_" + hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _should_add_page_fallback(
        self,
        page_text: str,
        caption_candidates: List[Tuple[Any, str, str]],
        page_candidates: List[VisualArtifactCandidate],
    ) -> bool:
        if caption_candidates or page_candidates:
            return False
        text = page_text or ""
        if _is_toc_or_list_page(text):
            return False
        if any(_looks_like_body_figure_table_reference(line) for line in _first_nonempty_lines(text, limit=20)):
            return False
        return bool(VISUAL_KEYWORD_RE.search(text))

    def _context_around_caption(
        self,
        extracted_document: ExtractedDocument,
        page_number: int,
        page_text: str,
        caption: str,
    ) -> Tuple[str, str]:
        page_map = {page.page: page.text for page in extracted_document.pages}
        index = page_text.find(caption) if caption else -1
        if index >= 0:
            before = page_text[max(0, index - 1200) : index]
            after = page_text[index + len(caption) : index + len(caption) + 1200]
        else:
            before = page_text[:1200]
            after = page_text[-1200:]
        previous_tail = page_map.get(page_number - 1, "")[-400:]
        next_head = page_map.get(page_number + 1, "")[:400]
        return ("\n".join(part for part in [previous_tail, before] if part).strip(), "\n".join(part for part in [after, next_head] if part).strip())

    def _section_path(self, page_text: str) -> List[str]:
        for line in (page_text or "").splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) > 120:
                continue
            if re.match(r"^\d+(?:\.\d+){0,8}\s+\S+", stripped):
                return [stripped]
        return []

    def _label(self, caption: str) -> str:
        match = re.search(r"(?:Figure|Fig\.?|Table)\s+\d+(?:[-.]\d+)*|图\s*\d+(?:[-.]\d+)*|表\s*\d+(?:[-.]\d+)*", caption or "", re.IGNORECASE)
        return match.group(0) if match else ""

    def _artifact_type_from_caption(self, caption: str) -> str:
        lower = (caption or "").lower()
        if "table" in lower or "表" in caption:
            return "table"
        if "timing" in lower or "时序" in caption:
            return "timing_diagram"
        if "state machine" in lower or "状态机" in caption:
            return "state_machine"
        if "waveform" in lower:
            return "waveform"
        if "bit field" in lower or "位域" in caption:
            return "bitfield"
        if "flow" in lower or "流程" in caption:
            return "flowchart"
        if "chart" in lower:
            return "chart"
        return "figure"

    def _area_ratio(self, rect: Any, page_area: float) -> float:
        return max(0.0, float(rect.width * rect.height)) / max(1.0, page_area)

    def _dedupe(self, candidates: List[VisualArtifactCandidate]) -> List[VisualArtifactCandidate]:
        result: List[VisualArtifactCandidate] = []
        for candidate in candidates:
            duplicate = False
            for existing in result:
                if candidate.page != existing.page:
                    continue
                same_image = candidate.image_hash == existing.image_hash
                same_caption = bool(candidate.caption and candidate.caption == existing.caption)
                if (same_image or same_caption) and bbox_iou(candidate.bbox, existing.bbox) > 0.75:
                    duplicate = True
                    break
                if candidate.artifact_type == existing.artifact_type and bbox_iou(candidate.bbox, existing.bbox) > 0.60:
                    duplicate = True
                    break
            if not duplicate:
                result.append(candidate)
        return result

    def _iou(self, a: Dict[str, Any], b: Dict[str, Any]) -> float:
        return bbox_iou(a, b)
def _is_toc_or_list_page(page_text: str) -> bool:
    lowered = (page_text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "table of contents",
            "list of figures",
            "list of tables",
            "revision history",
        )
    )


def _looks_like_toc_entry(text: str) -> bool:
    value = str(text or "")
    dotted = bool(re.search(r"\.{5,}\s*\d+\s*$", value))
    many_sections = len(re.findall(r"\b\d+(?:\.\d+)*\b", value)) >= 5 and value.count("\n") >= 3
    return dotted or value.count("....") >= 1 or many_sections


def _rect_overlap_ratio(inner: Any, outer: Any) -> float:
    intersection = inner & outer
    intersection_area = max(0.0, float(intersection.width * intersection.height))
    inner_area = max(1.0, float(inner.width * inner.height))
    return intersection_area / inner_area


def _union_rects(rects: Iterable[Any]) -> Any:
    rect_list = [rect for rect in rects if rect is not None]
    if not rect_list:
        return None
    try:
        import fitz
    except ImportError:
        return rect_list[0]
    x0 = min(float(rect.x0) for rect in rect_list)
    y0 = min(float(rect.y0) for rect in rect_list)
    x1 = max(float(rect.x1) for rect in rect_list)
    y1 = max(float(rect.y1) for rect in rect_list)
    return fitz.Rect(x0, y0, x1, y1)


def _expand_rect(rect: Any, page_rect: Any, *, y_padding: float) -> Any:
    if rect is None:
        return None
    try:
        import fitz
    except ImportError:
        return rect
    return fitz.Rect(
        max(float(page_rect.x0), float(rect.x0)),
        max(float(page_rect.y0), float(rect.y0) - y_padding),
        min(float(page_rect.x1), float(rect.x1)),
        min(float(page_rect.y1), float(rect.y1) + y_padding),
    )


def _dedupe_rects(rects: Iterable[Any]) -> List[Any]:
    result: List[Any] = []
    for rect in rects:
        if rect is None:
            continue
        duplicate = any(_rect_overlap_ratio(rect, existing) >= 0.82 for existing in result)
        if not duplicate:
            result.append(rect)
    return result


def _block_looks_table_like(text: str) -> bool:
    value = str(text or "")
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if not lines:
        return False
    header_hits = len(TABLE_HEADER_RE.findall(value))
    pipe_or_tab = sum(1 for line in lines if "|" in line or "\t" in line)
    dense_rows = sum(1 for line in lines if _line_looks_table_row(line))
    return bool(pipe_or_tab >= 2 or (header_hits >= 2 and dense_rows >= 2) or dense_rows >= 5)


def _line_looks_table_row(line: str) -> bool:
    tokens = str(line or "").split()
    if len(tokens) < 3:
        return False
    if "|" in line or "\t" in line:
        return True
    signalish = sum(1 for token in tokens if re.fullmatch(r"[A-Za-z0-9_./:\-\[\](),+<>|]+", token))
    numeric = sum(1 for token in tokens if re.search(r"\d", token))
    return bool(signalish >= max(2, len(tokens) - 1) or numeric >= 2)


def _page_edge_dense_table_like(blocks: List[Dict[str, Any]], page_rect: Any) -> bool:
    if len(blocks) < 3:
        return False
    top = any(float(block["rect"].y0) <= float(page_rect.y0) + float(page_rect.height) * 0.18 for block in blocks)
    bottom = any(float(block["rect"].y1) >= float(page_rect.y1) - float(page_rect.height) * 0.18 for block in blocks)
    return top or bottom


def _rect_touches_page_edge(rect: Any, page_rect: Any) -> bool:
    height = max(1.0, float(page_rect.height))
    return bool(float(rect.y0) <= float(page_rect.y0) + height * 0.18 or float(rect.y1) >= float(page_rect.y1) - height * 0.12)


def _table_continuation_hint(page_text: str, previous_page_text: str) -> bool:
    text = str(page_text or "")
    if TABLE_CONTINUATION_RE.search(text[:1200]):
        return True
    current_headers = set(token.lower() for token in TABLE_HEADER_RE.findall(text[:1200]))
    previous_headers = set(token.lower() for token in TABLE_HEADER_RE.findall(str(previous_page_text or "")[-1600:]))
    return bool(len(current_headers & previous_headers) >= 2 and _block_text_dense_table_like(text))


def _block_text_dense_table_like(text: str) -> bool:
    lines = [line for line in str(text or "").splitlines() if line.strip()]
    if len(lines) < 6:
        return False
    dense = sum(1 for line in lines[:40] if _line_looks_table_row(line))
    return dense >= 6 or len(TABLE_HEADER_RE.findall("\n".join(lines[:20]))) >= 4
