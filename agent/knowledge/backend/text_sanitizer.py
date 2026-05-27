"""Sanitize PDF text before it becomes ordinary knowledge chunks."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import DocumentPage
from .visual_extractors import CAPTION_RE


_SIGNALISH_RE = re.compile(r"^[A-Za-z0-9_./:\-\[\](),+<>|]+$")
_WORD_RE = re.compile(r"[A-Za-z][A-Za-z]{2,}")
_SECTION_HEADING_RE = re.compile(
    r"^\s*(?:#{1,6}\s+)?(?:\d+(?:\.\d+){0,8}|[A-Z][A-Z0-9 ]{2,})\s+[A-Za-z0-9][A-Za-z0-9 ,:;()_./+\-\[\]]{2,160}$"
)
_VISUAL_LABELS = {
    "sideband",
    "tx",
    "rx",
    "module",
    "layer",
    "clk",
    "clock",
    "valid",
    "data",
    "lane",
    "link",
    "adapter",
    "phy",
}


def sanitize_pages_for_knowledge_chunks(
    source_path: Path,
    pages: List[DocumentPage],
    *,
    enabled: bool = True,
    strip_visual_regions: bool = True,
    strip_visual_noise_lines: bool = True,
) -> Tuple[List[DocumentPage], Dict[str, Any]]:
    """Remove chart-internal PDF text from pages used for ordinary chunks."""

    source = Path(source_path)
    report: Dict[str, Any] = {
        "enabled": bool(enabled),
        "source_path": str(source),
        "pages": [],
        "removed_total_blocks": 0,
        "removed_total_lines": 0,
    }
    if not enabled or source.suffix.lower() != ".pdf":
        report["enabled"] = bool(enabled)
        return pages, report

    if strip_visual_regions:
        try:
            return _sanitize_pdf_pages_with_pymupdf(
                source,
                pages,
                report,
                strip_visual_regions=strip_visual_regions,
                strip_visual_noise_lines=strip_visual_noise_lines,
            )
        except ImportError:
            pass
        except Exception:
            # Text ingestion should remain available even when PyMuPDF cannot
            # parse a particular PDF. Fall back to conservative line filtering.
            pass

    sanitized_pages, line_report = _sanitize_pages_by_lines(
        source,
        pages,
        report,
        strip_visual_noise_lines=strip_visual_noise_lines,
    )
    return sanitized_pages, line_report


def is_visual_noise_line(line: str) -> bool:
    """Return True for lines that are clearly PDF chart-internal noise."""

    text = _normalize_line(line)
    if not text or CAPTION_RE.search(text):
        return False

    tokens = text.split()
    if len(tokens) >= 8:
        one_char_tokens = sum(1 for token in tokens if len(token) == 1 and token.isalnum())
        if one_char_tokens / max(1, len(tokens)) >= 0.55:
            return True

    compact = re.sub(r"\s+", "", text)
    if len(tokens) >= 6 and len(compact) <= 96:
        signalish = sum(1 for token in tokens if _is_signalish_token(token))
        natural = sum(1 for token in tokens if _looks_like_word(token))
        short_or_numeric = sum(1 for token in tokens if len(token) <= 2 or any(ch.isdigit() for ch in token))
        if signalish / max(1, len(tokens)) >= 0.75 and natural <= 1 and short_or_numeric / len(tokens) >= 0.5:
            return True

    if len(text) <= 96 and _looks_like_concatenated_signal_line(text):
        return True

    return False


def _sanitize_pdf_pages_with_pymupdf(
    source: Path,
    pages: List[DocumentPage],
    report: Dict[str, Any],
    *,
    strip_visual_regions: bool,
    strip_visual_noise_lines: bool,
) -> Tuple[List[DocumentPage], Dict[str, Any]]:
    import fitz

    page_by_number = {page.page: page for page in pages}
    sanitized: List[DocumentPage] = []
    with fitz.open(str(source)) as pdf:
        for page_number, pdf_page in enumerate(pdf, start=1):
            original = page_by_number.get(page_number, DocumentPage(page=page_number, text=""))
            blocks = _text_blocks(pdf_page)
            image_regions = _image_regions(pdf_page) if strip_visual_regions else []
            caption_regions, caption_block_indexes = _caption_regions(pdf_page.rect, blocks) if strip_visual_regions else ([], set())
            visual_regions = [*image_regions, *caption_regions]
            page_report = {
                "page": page_number,
                "original_chars": len(original.text or ""),
                "sanitized_chars": 0,
                "removed_blocks": 0,
                "removed_lines": 0,
                "kept_caption_lines": 0,
            }

            kept_parts: List[str] = []
            for index, block in enumerate(blocks):
                text = str(block.get("text") or "").strip()
                if not text:
                    continue
                in_visual_region = _rect_overlaps_any(block["rect"], visual_regions, threshold=0.55)
                is_caption_block = index in caption_block_indexes or bool(CAPTION_RE.search(text))
                if (
                    in_visual_region
                    and not is_caption_block
                    and not _is_section_heading(text)
                    and not _is_natural_language_block(text)
                ):
                    page_report["removed_lines"] += max(1, len([line for line in text.splitlines() if line.strip()]))
                    page_report["removed_blocks"] += 1
                    continue

                cleaned_lines, removed_lines, kept_caption_lines = _sanitize_lines(
                    text,
                    strip_visual_noise_lines=strip_visual_noise_lines,
                    in_visual_region=in_visual_region,
                )
                page_report["removed_lines"] += removed_lines
                page_report["kept_caption_lines"] += kept_caption_lines
                if cleaned_lines:
                    kept_parts.append("\n".join(cleaned_lines))

            sanitized_text = "\n\n".join(part for part in kept_parts if part.strip()).strip()
            if not sanitized_text and original.text:
                cleaned_lines, removed_lines, kept_caption_lines = _sanitize_lines(
                    original.text,
                    strip_visual_noise_lines=strip_visual_noise_lines,
                    in_visual_region=False,
                )
                sanitized_text = "\n".join(cleaned_lines).strip()
                page_report["removed_lines"] += removed_lines
                page_report["kept_caption_lines"] += kept_caption_lines

            page_report["sanitized_chars"] = len(sanitized_text)
            report["removed_total_blocks"] += int(page_report["removed_blocks"])
            report["removed_total_lines"] += int(page_report["removed_lines"])
            report["pages"].append(page_report)
            sanitized.append(replace(original, text=sanitized_text))

    if not sanitized:
        sanitized = pages
    return sanitized, report


def _sanitize_pages_by_lines(
    source: Path,
    pages: List[DocumentPage],
    report: Dict[str, Any],
    *,
    strip_visual_noise_lines: bool,
) -> Tuple[List[DocumentPage], Dict[str, Any]]:
    sanitized: List[DocumentPage] = []
    for page in pages:
        cleaned_lines, removed_lines, kept_caption_lines = _sanitize_lines(
            page.text,
            strip_visual_noise_lines=strip_visual_noise_lines,
            in_visual_region=False,
        )
        text = "\n".join(cleaned_lines).strip()
        page_report = {
            "page": page.page,
            "original_chars": len(page.text or ""),
            "sanitized_chars": len(text),
            "removed_blocks": 0,
            "removed_lines": removed_lines,
            "kept_caption_lines": kept_caption_lines,
        }
        report["removed_total_lines"] += removed_lines
        report["pages"].append(page_report)
        sanitized.append(replace(page, text=text))
    return sanitized, report


def _sanitize_lines(
    text: str,
    *,
    strip_visual_noise_lines: bool,
    in_visual_region: bool,
) -> Tuple[List[str], int, int]:
    kept: List[str] = []
    removed = 0
    kept_caption = 0
    for raw_line in str(text or "").splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue
        if CAPTION_RE.search(line):
            kept.append(line)
            kept_caption += 1
            continue
        if strip_visual_noise_lines and is_visual_noise_line(line):
            removed += 1
            continue
        if in_visual_region and _is_visual_region_label_line(line):
            removed += 1
            continue
        kept.append(line)
    return kept, removed, kept_caption


def _text_blocks(page: Any) -> List[Dict[str, Any]]:
    blocks: List[Dict[str, Any]] = []
    for raw in page.get_text("blocks") or []:
        if len(raw) < 5:
            continue
        text = str(raw[4] or "").strip()
        if not text:
            continue
        blocks.append(
            {
                "rect": _rect_from_tuple(raw[:4]),
                "bbox": tuple(float(value) for value in raw[:4]),
                "text": text,
            }
        )
    blocks.sort(key=lambda item: (item["bbox"][1], item["bbox"][0]))
    return blocks


def _image_regions(page: Any) -> List[Any]:
    regions: List[Any] = []
    for image in page.get_images(full=True) or []:
        xref = image[0]
        for rect in page.get_image_rects(xref) or []:
            regions.append(rect)
    return regions


def _caption_regions(page_rect: Any, blocks: List[Dict[str, Any]]) -> Tuple[List[Any], set[int]]:
    regions: List[Any] = []
    caption_indexes: set[int] = set()
    for index, block in enumerate(blocks):
        text = block["text"]
        if not CAPTION_RE.search(text):
            continue
        caption_indexes.add(index)
        caption_rect = block["rect"]
        height = max(page_rect.height * 0.22, caption_rect.height * 7)
        top = max(page_rect.y0, caption_rect.y0 - height * 0.65)
        bottom = min(page_rect.y1, caption_rect.y1 + height * 0.65)
        regions.append(_new_rect(page_rect.x0, top, page_rect.x1, bottom, template=page_rect))
    return regions, caption_indexes


def _rect_from_tuple(values: Iterable[Any]) -> Any:
    import fitz

    x0, y0, x1, y1 = [float(value) for value in values]
    return fitz.Rect(x0, y0, x1, y1)


def _new_rect(x0: float, y0: float, x1: float, y1: float, *, template: Any) -> Any:
    import fitz

    return fitz.Rect(float(x0), float(y0), float(x1), float(y1))


def _rect_overlaps_any(rect: Any, regions: Iterable[Any], *, threshold: float) -> bool:
    for region in regions:
        if _overlap_ratio(rect, region) >= threshold:
            return True
    return False


def _overlap_ratio(a: Any, b: Any) -> float:
    intersection = a & b
    intersection_area = max(0.0, float(intersection.width * intersection.height))
    area = max(1.0, float(a.width * a.height))
    return intersection_area / area


def _normalize_line(line: str) -> str:
    return re.sub(r"[ \t]+", " ", str(line or "").strip())


def _is_signalish_token(token: str) -> bool:
    value = token.strip()
    if not value:
        return False
    if len(value) == 1 and value.isalnum():
        return True
    if re.search(r"\d", value) and _SIGNALISH_RE.match(value):
        return True
    if "_" in value or "[" in value or "]" in value:
        return True
    if value.lower() in _VISUAL_LABELS:
        return True
    return bool(_SIGNALISH_RE.match(value) and len(value) <= 14 and value.upper() == value)


def _looks_like_word(token: str) -> bool:
    return bool(_WORD_RE.fullmatch(token.strip()))


def _looks_like_concatenated_signal_line(text: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9_]", "", text)
    if len(compact) < 16:
        return False
    lowered = compact.lower()
    marker_count = sum(1 for marker in ("rx", "tx", "clk", "ck", "data", "sb", "valid", "vld") if marker in lowered)
    natural_words = _WORD_RE.findall(text)
    return marker_count >= 3 and len(natural_words) <= 2


def _is_visual_region_label_line(line: str) -> bool:
    text = _normalize_line(line)
    if not text or CAPTION_RE.search(text) or _is_section_heading(text):
        return False
    tokens = text.split()
    if len(tokens) <= 4 and len(text) <= 48:
        words = [token for token in tokens if re.fullmatch(r"[A-Za-z][A-Za-z0-9_/-]*", token)]
        if words and all(len(word) <= 16 for word in words):
            return True
    if len(text) <= 96:
        signalish = sum(1 for token in tokens if _is_signalish_token(token))
        if signalish >= max(1, len(tokens) - 1):
            return True
    return False


def _is_section_heading(text: str) -> bool:
    first_line = _normalize_line(str(text or "").splitlines()[0] if text else "")
    if not first_line or len(first_line) > 180 or CAPTION_RE.search(first_line):
        return False
    if _SECTION_HEADING_RE.match(first_line):
        return True
    return bool(re.match(r"^\s*\d+(?:\.\d+){1,8}\s+\S+", first_line))


def _is_natural_language_block(text: str) -> bool:
    compact = " ".join(str(text or "").split())
    if len(compact) < 80:
        return False
    words = _WORD_RE.findall(compact)
    if len(words) < 10:
        return False
    signalish_tokens = [_is_signalish_token(token) for token in compact.split()]
    signalish_ratio = sum(1 for item in signalish_tokens if item) / max(1, len(signalish_tokens))
    return signalish_ratio < 0.45
