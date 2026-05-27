"""Geometry helpers for visual artifact grouping and deduplication."""

from __future__ import annotations

from typing import Any, Mapping


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
