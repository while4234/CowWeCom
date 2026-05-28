"""Sanitize PDF text before it becomes ordinary knowledge chunks."""

from __future__ import annotations

import re
from dataclasses import replace
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .models import DocumentPage
from .visual_extractors import (
    _next_line_can_be_caption_title,
    is_caption_label_line,
    is_strict_caption_block,
    normalize_caption_text,
    _is_toc_or_list_page,
)


_SIGNALISH_RE = re.compile(r"^[A-Za-z0-9_./:\-\[\](),+<>|]+$")
_SIGNAL_NAME_WITH_RANGES_RE = re.compile(
    r"^[A-Za-z][A-Za-z0-9_]*(?:\[[A-Za-z0-9_+\-*/():]+\])*$"
)
_BIT_FIELD_ROW_RE = re.compile(
    r"^\s*\[[A-Za-z0-9_+\-*/():]+\]\s*:\s*"
    r"(?:Reserved|Valid|Invalid|Enable|Disable|Value|Encoding|Field|Stack|PCIe|CXL|Streaming|[A-Za-z0-9_].*)$",
    re.IGNORECASE,
)
_ENCODING_VALUE_ROW_RE = re.compile(
    r"^\s*(?:0x[0-9A-Fa-f]+|[0-9A-Fa-f]+h|[01]{1,8}|[A-Za-z][A-Za-z0-9_]*)\s*:\s*"
    r"(?:Reserved|Valid|Invalid|Stack|PCIe|CXL|Streaming|Protocol|Message|[A-Za-z0-9_].*)$",
    re.IGNORECASE,
)
_SHEET_COUNT_RE = re.compile(r"^\(?\s*sheet\s+\d+\s+of\s+\d+\s*\)?$", re.IGNORECASE)
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
_MATH_KEYWORDS = {
    "ceil",
    "crc",
    "equation",
    "floor",
    "formula",
    "int",
    "log",
    "loss",
    "polynomial",
    "vtf",
}
_FORMULA_CONTEXT_RE = re.compile(
    r"\b(?:equation|formula|loss|polynomial|crc|vtf|burst\s+address|transfer\s+function|log|ceil|floor|int)\b|公式|方程",
    re.IGNORECASE,
)
_FORMULA_TOKEN_RE = re.compile(r"[A-Za-z]+|\d+(?:\.\d+)?|[=+\-*/^(){}\[\]|<>]|[^\s]")
_TABLE_HEADER_WORDS = {
    "bit",
    "bits",
    "description",
    "direction",
    "encoding",
    "field",
    "layer",
    "meaning",
    "message",
    "module",
    "msgcode",
    "msginfo",
    "msgsubcode",
    "name",
    "parameter",
    "register",
    "reserved",
    "signal",
    "type",
    "value",
    "width",
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
    if not text or is_strict_caption_block(text) or is_caption_label_line(text):
        return False
    if is_formula_garble_line(text):
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


def is_formula_garble_line(line: str, context: str = "") -> bool:
    """Return True for PDF text extraction fragments that look like broken math."""

    text = _normalize_line(line)
    if not text or is_strict_caption_block(text) or is_caption_label_line(text):
        return False
    if _is_signal_or_encoding_table_line(text, context=context):
        return False
    tokens = _FORMULA_TOKEN_RE.findall(text)
    if len(tokens) < 6:
        return False

    words = [token for token in tokens if re.fullmatch(r"[A-Za-z]+", token)]
    natural_words = [
        word
        for word in words
        if len(word) >= 4 and word.lower() not in _MATH_KEYWORDS and not _is_signalish_token(word)
    ]
    if len(natural_words) >= 4:
        return False

    alnum_tokens = [token for token in tokens if re.fullmatch(r"[A-Za-z0-9]+", token)]
    single_char_tokens = [token for token in alnum_tokens if len(token) == 1]
    single_char_ratio = len(single_char_tokens) / max(1, len(alnum_tokens))
    math_symbols = sum(1 for token in tokens if re.fullmatch(r"[=+\-*/^(){}\[\]|<>]", token))
    numbers = sum(1 for token in tokens if re.fullmatch(r"\d+(?:\.\d+)?", token))
    keyword_hits = sum(1 for word in words if word.lower() in _MATH_KEYWORDS)
    repeated_parentheses = text.count("(") + text.count(")")
    context_hint = bool(_FORMULA_CONTEXT_RE.search(f"{context}\n{text}"))
    math_score = math_symbols + min(numbers, 4) + keyword_hits * 2 + min(repeated_parentheses, 4)

    if "=" in text and repeated_parentheses >= 2 and single_char_ratio >= 0.28 and len(natural_words) <= 2:
        return True
    if single_char_ratio >= 0.45 and math_score >= (4 if context_hint else 6) and len(natural_words) <= 2:
        return True
    if context_hint and single_char_ratio >= 0.35 and math_score >= 5 and len(natural_words) <= 2:
        return True
    return False


def is_formula_garble_block(text: str) -> bool:
    """Return True when a block contains likely broken formula extraction."""

    value = str(text or "")
    if not value.strip():
        return False
    if _looks_like_signal_or_encoding_table_block(value):
        return False
    context_hint = bool(_FORMULA_CONTEXT_RE.search(value))
    lines = [_normalize_line(line) for line in value.splitlines() if _normalize_line(line)]
    if not lines:
        return False
    matches = [line for line in lines if is_formula_garble_line(line, context=value)]
    if matches:
        return True
    if not context_hint:
        return False
    compact = " ".join(lines)
    tokens = _FORMULA_TOKEN_RE.findall(compact)
    if len(tokens) < 10:
        return False
    alnum_tokens = [token for token in tokens if re.fullmatch(r"[A-Za-z0-9]+", token)]
    single_char_ratio = sum(1 for token in alnum_tokens if len(token) == 1) / max(1, len(alnum_tokens))
    natural_words = [
        token
        for token in tokens
        if re.fullmatch(r"[A-Za-z]{4,}", token)
        and token.lower() not in _MATH_KEYWORDS
        and not _is_signalish_token(token)
    ]
    math_symbols = sum(1 for token in tokens if re.fullmatch(r"[=+\-*/^(){}\[\]|<>]", token))
    return bool(single_char_ratio >= 0.34 and math_symbols >= 4 and len(natural_words) <= 4)


def is_large_table_like_block(text: str) -> bool:
    """Return True for oversized dense table text that should be visual-first."""

    lines = [_normalize_line(line) for line in str(text or "").splitlines() if _normalize_line(line)]
    if len(lines) < 8:
        return False
    if any(is_strict_caption_block(line) for line in lines[:3]) and len(lines) < 10:
        return False

    table_lines = sum(1 for line in lines if _is_table_like_line(line))
    pipe_or_tab_lines = sum(1 for line in lines if "|" in line or "\t" in line)
    header_hits = len(_table_header_tokens("\n".join(lines[:20])))
    short_dense_lines = sum(1 for line in lines if _is_dense_short_table_row(line))
    natural_sentences = sum(1 for line in lines if _is_natural_language_line(line))
    density = table_lines / max(1, len(lines))

    if pipe_or_tab_lines >= 4 and len(lines) >= 8:
        return True
    if header_hits >= 2 and len(lines) >= 12 and density >= 0.45 and natural_sentences <= max(2, len(lines) // 5):
        return True
    if short_dense_lines >= 10 and natural_sentences <= 2:
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
            if _is_toc_or_list_page(original.text or ""):
                page_report = {
                    "page": page_number,
                    "original_chars": len(original.text or ""),
                    "sanitized_chars": len(original.text or ""),
                    "removed_blocks": 0,
                    "removed_lines": 0,
                    "kept_caption_lines": 0,
                }
                report["pages"].append(page_report)
                sanitized.append(original)
                continue
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
                is_caption_block = index in caption_block_indexes or is_strict_caption_block(text)
                if (
                    in_visual_region
                    and not is_caption_block
                    and not _is_section_heading(text)
                    and not _is_natural_language_block(text)
                    and not is_formula_garble_block(text)
                ):
                    page_report["removed_lines"] += max(1, len([line for line in text.splitlines() if line.strip()]))
                    page_report["removed_blocks"] += 1
                    continue

                cleaned_lines, removed_lines, kept_caption_lines = _sanitize_lines(
                    text,
                    strip_visual_noise_lines=strip_visual_noise_lines,
                    in_visual_region=in_visual_region and not is_caption_block,
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
    keep_next_caption_title = False
    for raw_line in str(text or "").splitlines():
        line = _normalize_line(raw_line)
        if not line:
            continue
        if keep_next_caption_title:
            kept.append(line)
            kept_caption += 1
            keep_next_caption_title = False
            continue
        if is_caption_label_line(line):
            kept.append(line)
            kept_caption += 1
            keep_next_caption_title = True
            continue
        if is_strict_caption_block(line):
            kept.append(normalize_caption_text(line))
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
        text = _caption_text_from_block(blocks, index)
        if not is_strict_caption_block(text):
            continue
        caption_indexes.add(index)
        original_lines = [_normalize_line(line) for line in str(block["text"] or "").splitlines() if _normalize_line(line)]
        if original_lines and is_caption_label_line(original_lines[0]) and len(original_lines) < 2 and index + 1 < len(blocks):
            caption_indexes.add(index + 1)
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


def _table_header_tokens(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_/-]{1,32}", str(text or ""))
        if token.lower() in _TABLE_HEADER_WORDS
    }


def _is_table_like_line(line: str) -> bool:
    text = _normalize_line(line)
    if not text or is_strict_caption_block(text) or is_caption_label_line(text):
        return False
    if _is_signal_or_encoding_table_line(text):
        return True
    if "|" in text or "\t" in text:
        return True
    tokens = text.split()
    if len(tokens) < 3:
        return False
    header_hits = sum(1 for token in tokens if token.lower().strip(":") in _TABLE_HEADER_WORDS)
    if header_hits >= 2:
        return True
    signalish = sum(1 for token in tokens if _is_signalish_token(token))
    numeric = sum(1 for token in tokens if re.search(r"\d", token))
    natural = sum(1 for token in tokens if _looks_like_word(token))
    return bool(len(tokens) >= 4 and signalish + numeric >= 3 and natural <= max(2, len(tokens) // 3))


def _is_dense_short_table_row(line: str) -> bool:
    text = _normalize_line(line)
    if not text or len(text) > 180:
        return False
    if _is_signal_or_encoding_table_line(text):
        return True
    tokens = text.split()
    if len(tokens) < 3:
        return False
    signalish = sum(1 for token in tokens if _is_signalish_token(token))
    separators = text.count("|") + text.count("\t") + len(re.findall(r"\s{2,}", text))
    return bool(signalish >= max(2, len(tokens) - 2) or separators >= 2)


def _is_natural_language_line(line: str) -> bool:
    text = _normalize_line(line)
    if len(text) < 40:
        return False
    words = _WORD_RE.findall(text)
    return bool(len(words) >= 7 and re.search(r"[.!?。；;]\s*$", text))


def _looks_like_concatenated_signal_line(text: str) -> bool:
    compact = re.sub(r"[^A-Za-z0-9_]", "", text)
    if len(compact) < 16:
        return False
    lowered = compact.lower()
    marker_count = sum(1 for marker in ("rx", "tx", "clk", "ck", "data", "sb", "valid", "vld") if marker in lowered)
    natural_words = _WORD_RE.findall(text)
    return marker_count >= 3 and len(natural_words) <= 2


def _is_signal_or_encoding_table_line(line: str, context: str = "") -> bool:
    text = _normalize_line(line)
    if not text:
        return False
    if _SHEET_COUNT_RE.fullmatch(text):
        return True
    if _BIT_FIELD_ROW_RE.match(text) or _ENCODING_VALUE_ROW_RE.match(text):
        return True

    normalized = re.sub(r"\s*\|\s*", " | ", text)
    tokens = [token.strip(",:;") for token in normalized.split() if token != "|"]
    if not tokens:
        return False
    header_hits = sum(1 for token in tokens if token.lower().strip(":") in _TABLE_HEADER_WORDS)
    if header_hits >= 2:
        return True

    first = tokens[0]
    first_is_signal = bool(_SIGNAL_NAME_WITH_RANGES_RE.fullmatch(first))
    first_has_signal_shape = (
        "_" in first
        or "[" in first
        or first.lower().startswith(("lp_", "pl_"))
        or first.lower() in {"msginfo", "msgcode", "msgsubcode"}
    )
    has_bit_range = bool(re.search(r"\[[A-Za-z0-9_+\-*/():]+\]", text))
    has_table_context = len(_table_header_tokens(context)) >= 2 or bool(
        re.search(r"\b(?:signal\s+list|message\s+encodings?|register|bit\s+field)\b", context, re.IGNORECASE)
    )
    natural_words = [
        token
        for token in re.findall(r"[A-Za-z]{4,}", text)
        if token.lower() not in _MATH_KEYWORDS and not _is_signalish_token(token)
    ]
    if first_is_signal and first_has_signal_shape and (has_bit_range or len(tokens) >= 2) and (natural_words or has_table_context):
        return True
    return False


def _looks_like_signal_or_encoding_table_block(text: str) -> bool:
    value = str(text or "")
    lines = [_normalize_line(line) for line in value.splitlines() if _normalize_line(line)]
    if not lines:
        return False
    if any(_SHEET_COUNT_RE.fullmatch(line) for line in lines) and re.search(
        r"\b(?:table|signal\s+list|message\s+encodings?)\b", value, re.IGNORECASE
    ):
        return True
    header_hits = len(_table_header_tokens(value))
    row_hits = sum(1 for line in lines if _is_signal_or_encoding_table_line(line, context=value))
    if row_hits >= 2:
        return True
    return bool(header_hits >= 2 and row_hits >= 1)


def _is_visual_region_label_line(line: str) -> bool:
    text = _normalize_line(line)
    if not text or is_caption_label_line(text) or is_strict_caption_block(text) or _is_section_heading(text):
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
    if not first_line or len(first_line) > 180 or is_caption_label_line(first_line) or is_strict_caption_block(first_line):
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


def _caption_text_from_block(blocks: List[Dict[str, Any]], index: int) -> str:
    text = str(blocks[index].get("text") or "")
    lines = [_normalize_line(line) for line in text.splitlines() if _normalize_line(line)]
    if lines and is_caption_label_line(lines[0]) and len(lines) < 2 and index + 1 < len(blocks):
        next_text = str(blocks[index + 1].get("text") or "")
        next_lines = [_normalize_line(line) for line in next_text.splitlines() if _normalize_line(line)]
        if next_lines and _next_line_can_be_caption_title(next_lines[0]):
            return f"{text.rstrip()}\n{next_lines[0]}"
    return text
