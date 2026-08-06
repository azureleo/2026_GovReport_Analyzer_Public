from __future__ import annotations

from .models import BBox


def normalize_bbox(bbox: BBox | None, width: float, height: float) -> BBox | None:
    if bbox is None or width <= 0 or height <= 0:
        return None
    x0, y0, x1, y1 = bbox
    return (
        max(0.0, min(999.0, x0 / width * 999.0)),
        max(0.0, min(999.0, y0 / height * 999.0)),
        max(0.0, min(999.0, x1 / width * 999.0)),
        max(0.0, min(999.0, y1 / height * 999.0)),
    )


def denormalize_bbox(bbox: BBox | None, width: float, height: float) -> BBox | None:
    if bbox is None or width <= 0 or height <= 0:
        return None
    x0, y0, x1, y1 = bbox
    return (
        x0 / 999.0 * width,
        y0 / 999.0 * height,
        x1 / 999.0 * width,
        y1 / 999.0 * height,
    )


def bbox_iou(left: BBox | None, right: BBox | None) -> float:
    if left is None or right is None:
        return 0.0
    lx0, ly0, lx1, ly1 = left
    rx0, ry0, rx1, ry1 = right
    ix0, iy0 = max(lx0, rx0), max(ly0, ry0)
    ix1, iy1 = min(lx1, rx1), min(ly1, ry1)
    intersection = max(0.0, ix1 - ix0) * max(0.0, iy1 - iy0)
    left_area = max(0.0, lx1 - lx0) * max(0.0, ly1 - ly0)
    right_area = max(0.0, rx1 - rx0) * max(0.0, ry1 - ry0)
    union = left_area + right_area - intersection
    return intersection / union if union > 0 else 0.0

