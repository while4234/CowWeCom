"""Visual artifact extraction for protocol knowledge backend PDFs."""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from common.log import logger

from .models import ExtractedDocument, KnowledgeDocument, VisualArtifactCandidate
from .storage import stable_visual_artifact_id


STRICT_CAPTION_RE = re.compile(
    r"(?im)^\s*(?:Figure|Fig\.?|Table)\s+\d+(?:[-.]\d+)*\s*[.:：-]?\s+\S+|"
    r"^\s*[图表]\s*\d+(?:[-.]\d+)*\s*[.:：-]?\s+\S+"
)
VISUAL_KEYWORD_RE = re.compile(
    r"\b(?:timing|waveform|state\s*machine|bit\s*field|diagram|chart)\b|时序|状态机|流程图|位域",
    re.IGNORECASE,
)
CAPTION_RE = STRICT_CAPTION_RE


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

                for rect in self._image_rects(page):
                    if self._area_ratio(rect, page_area) < min_area_ratio:
                        continue
                    candidates.append(
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
                            parser_confidence=0.70,
                        )
                    )

                caption_candidates = self._caption_candidates(page_rect, text_blocks)
                for rect, caption, artifact_type in caption_candidates:
                    if self._area_ratio(rect, page_area) < min_area_ratio:
                        continue
                    candidates.append(
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
                            caption=caption,
                            parser_confidence=0.85,
                        )
                    )

                if self._should_add_page_fallback(page_text, caption_candidates, candidates, page_index):
                    candidates.append(
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
                            caption="",
                            parser_confidence=0.55,
                        )
                    )
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

    def _caption_candidates(self, page_rect: Any, blocks: List[Dict[str, Any]]) -> List[Tuple[Any, str, str]]:
        try:
            import fitz
        except ImportError:
            return []
        candidates = []
        for block in blocks:
            text = block["text"]
            if _looks_like_toc_entry(text) or not STRICT_CAPTION_RE.search(text):
                continue
            x0, y0, x1, y1 = block["bbox"]
            caption_rect = fitz.Rect(x0, y0, x1, y1)
            height = max(page_rect.height * 0.22, caption_rect.height * 7)
            top = max(page_rect.y0, y0 - height * 0.65)
            bottom = min(page_rect.y1, y1 + height * 0.65)
            rect = fitz.Rect(page_rect.x0, top, page_rect.x1, bottom)
            candidates.append((rect, text[:300], self._artifact_type_from_caption(text)))
        return candidates

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
            "unit": "pdf_points",
        }
        context_before, context_after = self._context_around_caption(
            extracted_document,
            page_number,
            page_text,
            caption,
        )
        context_hash = hashlib.sha256(
            "\n".join([caption, context_before, context_after, page_text[:2000]]).encode("utf-8")
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
        candidates: List[VisualArtifactCandidate],
        page_index: int,
    ) -> bool:
        if caption_candidates or any(candidate.page == page_index for candidate in candidates):
            return False
        text = page_text or ""
        if _is_toc_or_list_page(text):
            return False
        return bool(STRICT_CAPTION_RE.search(text) and VISUAL_KEYWORD_RE.search(text))

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
        match = re.search(r"(?:Figure|Fig\.?|Table)\s+\d+(?:[-.]\d+)*|图\s*\d+|表\s*\d+", caption or "", re.IGNORECASE)
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
                if candidate.image_hash == existing.image_hash and self._iou(candidate.bbox, existing.bbox) > 0.85:
                    duplicate = True
                    break
            if not duplicate:
                result.append(candidate)
        return result

    def _iou(self, a: Dict[str, Any], b: Dict[str, Any]) -> float:
        ax0, ay0, ax1, ay1 = float(a.get("x0", 0)), float(a.get("y0", 0)), float(a.get("x1", 0)), float(a.get("y1", 0))
        bx0, by0, bx1, by1 = float(b.get("x0", 0)), float(b.get("y0", 0)), float(b.get("x1", 0)), float(b.get("y1", 0))
        ix0, iy0 = max(ax0, bx0), max(ay0, by0)
        ix1, iy1 = min(ax1, bx1), min(ay1, by1)
        intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
        area_a = max(0.0, ax1 - ax0) * max(0.0, ay1 - ay0)
        area_b = max(0.0, bx1 - bx0) * max(0.0, by1 - by0)
        union = area_a + area_b - intersection
        return intersection / union if union else 0.0


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
