from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import fitz

from .geometry import normalize_bbox
from .io_utils import load_manifest, save_objects
from .models import ExperimentObject


_TABLE_CAPTION_RE = re.compile(r"(?:^|\s|[\[(])(표\s*\d+(?:[-–]\d+)?)\s*([^\n]{0,100})")
_FIGURE_CAPTION_RE = re.compile(
    r"(?:^|\s|[\[(])((?:그림|도표|그래프|Figure|Fig\.?)\s*\d+(?:[-–]\d+)?)\s*([^\n]{0,100})",
    re.I,
)


def _clean_cell(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _table_candidates(page: fitz.Page) -> list[Any]:
    for strategy in ("lines_strict", "lines", "text"):
        try:
            tables = list(page.find_tables(strategy=strategy).tables)
        except Exception:
            tables = []
        valid = []
        for table in tables:
            try:
                rows = table.extract()
            except Exception:
                continue
            if rows and any(any(_clean_cell(cell) for cell in row) for row in rows):
                valid.append(table)
        if valid:
            return valid
    return []


def _nearby_caption(page: fitz.Page, bbox: tuple[float, float, float, float], kind: str) -> tuple[str, str]:
    x0, y0, x1, _ = bbox
    area = fitz.Rect(max(0, x0 - 20), max(0, y0 - 90), min(page.rect.width, x1 + 20), y0 + 10)
    text = page.get_textbox(area) or ""
    pattern = _TABLE_CAPTION_RE if kind == "table" else _FIGURE_CAPTION_RE
    matches = list(pattern.finditer(text))
    if not matches:
        return "", ""
    match = matches[-1]
    number = re.sub(r"\s+", " ", match.group(1)).strip()
    caption = f"{number} {_clean_cell(match.group(2))}".strip()
    return number, caption


def extract_baseline(manifest_path: str | Path) -> tuple[Path, list[ExperimentObject]]:
    manifest = load_manifest(manifest_path)
    run_dir = Path(manifest_path).resolve().parent
    output_dir = run_dir / "outputs" / "pymupdf"
    output_dir.mkdir(parents=True, exist_ok=True)
    selected = {int(item["page_number"]) for item in manifest["pages"]}
    objects: list[ExperimentObject] = []

    document = fitz.open(str(manifest["pdf_path"]))
    try:
        for page_number in sorted(selected):
            page = document[page_number - 1]
            page_width, page_height = float(page.rect.width), float(page.rect.height)
            sequence_by_type: dict[str, int] = {}

            def next_sequence(object_type: str) -> int:
                sequence_by_type[object_type] = sequence_by_type.get(object_type, 0) + 1
                return sequence_by_type[object_type]

            blocks = page.get_text("blocks") or []
            for block in blocks:
                if len(block) < 7 or int(block[6]) != 0:
                    continue
                text = _clean_cell(block[4])
                if not text:
                    continue
                bbox = (float(block[0]), float(block[1]), float(block[2]), float(block[3]))
                seq = next_sequence("text")
                objects.append(ExperimentObject(
                    object_id=f"pymupdf-p{page_number}-text-{seq}",
                    engine="pymupdf",
                    page_number=page_number,
                    object_type="text",
                    sequence=seq,
                    text=text,
                    bbox_pdf=bbox,
                    bbox_norm=normalize_bbox(bbox, page_width, page_height),
                    confidence="native",
                ))

            for table in _table_candidates(page):
                try:
                    rows = [[_clean_cell(cell) for cell in row] for row in table.extract()]
                except Exception:
                    continue
                bbox = (
                    float(table.bbox[0]),
                    float(table.bbox[1]),
                    float(table.bbox[2]),
                    float(table.bbox[3]),
                )
                number, caption = _nearby_caption(page, bbox, "table")
                seq = next_sequence("table")
                objects.append(ExperimentObject(
                    object_id=f"pymupdf-p{page_number}-table-{seq}",
                    engine="pymupdf",
                    page_number=page_number,
                    object_type="table",
                    sequence=seq,
                    rows=rows,
                    caption=caption,
                    number=number,
                    bbox_pdf=bbox,
                    bbox_norm=normalize_bbox(bbox, page_width, page_height),
                    confidence="deterministic",
                    metadata={
                        "row_count": len(rows),
                        "column_count": max((len(row) for row in rows), default=0),
                    },
                ))

            page_text = page.get_text("text") or ""
            for match in _FIGURE_CAPTION_RE.finditer(page_text):
                number = re.sub(r"\s+", " ", match.group(1)).strip()
                caption = f"{number} {_clean_cell(match.group(2))}".strip()
                seq = next_sequence("figure")
                objects.append(ExperimentObject(
                    object_id=f"pymupdf-p{page_number}-figure-{seq}",
                    engine="pymupdf",
                    page_number=page_number,
                    object_type="figure",
                    sequence=seq,
                    caption=caption,
                    number=number,
                    confidence="caption_only",
                ))

            for image in page.get_images(full=True):
                xref = int(image[0])
                try:
                    rects = page.get_image_rects(xref)
                except Exception:
                    rects = []
                for rect in rects[:1]:
                    if rect.width < 80 or rect.height < 80:
                        continue
                    bbox = (float(rect.x0), float(rect.y0), float(rect.x1), float(rect.y1))
                    number, caption = _nearby_caption(page, bbox, "figure")
                    seq = next_sequence("image")
                    objects.append(ExperimentObject(
                        object_id=f"pymupdf-p{page_number}-image-{seq}",
                        engine="pymupdf",
                        page_number=page_number,
                        object_type="image",
                        sequence=seq,
                        caption=caption,
                        number=number,
                        bbox_pdf=bbox,
                        bbox_norm=normalize_bbox(bbox, page_width, page_height),
                        confidence="native",
                        metadata={"xref": xref},
                    ))
    finally:
        document.close()

    output_path = output_dir / "objects.jsonl"
    save_objects(output_path, objects)
    return output_path, objects
