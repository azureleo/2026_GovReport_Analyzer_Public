from __future__ import annotations

import ast
import html
import re
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable

from .geometry import denormalize_bbox
from .io_utils import load_manifest, save_objects
from .models import ExperimentObject


_PAIR_RE = re.compile(
    r"<\|ref\|>(.*?)<\|/ref\|>\s*<\|det\|>(.*?)<\|/det\|>",
    re.DOTALL,
)
_DET_RE = re.compile(r"<\|det\|>(.*?)<\|/det\|>", re.DOTALL)
_OFFICIAL_DET_LINE_RE = re.compile(
    r"^\s*<\|det\|>([^<\s]+)(?:\s*(\[[^\n]*\]))?\s*<\|/det\|>(.*)$"
)
_PAGE_NUMBER_RE = re.compile(r"(?:page|p)[_-]?(\d+)", re.I)
_TABLE_SEPARATOR_RE = re.compile(r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$")


class _TableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag: str, attrs) -> None:
        if tag.lower() == "tr":
            self._row = []
        elif tag.lower() in {"td", "th"}:
            self._cell = []

    def handle_data(self, data: str) -> None:
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in {"td", "th"} and self._row is not None:
            value = re.sub(r"\s+", " ", html.unescape(" ".join(self._cell or []))).strip()
            self._row.append(value)
            self._cell = None
        elif tag.lower() == "tr" and self._row is not None:
            if any(self._row):
                self.rows.append(self._row)
            self._row = None


def _find_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, (list, tuple)):
        if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
            return (float(value[0]), float(value[1]), float(value[2]), float(value[3]))
        for item in value:
            bbox = _find_bbox(item)
            if bbox:
                return bbox
    if isinstance(value, dict):
        for key in ("bbox", "box", "coordinate", "coordinates"):
            bbox = _find_bbox(value.get(key))
            if bbox:
                return bbox
    return None


def parse_detection(value: str) -> tuple[str, tuple[float, float, float, float] | None]:
    """모델 출력을 실행하지 않고 좌표와 선택적 유형 라벨을 읽는다."""
    try:
        parsed = ast.literal_eval(value.strip())
    except (ValueError, SyntaxError):
        numbers = re.findall(r"-?\d+(?:\.\d+)?", value)
        bbox = (
            (float(numbers[0]), float(numbers[1]), float(numbers[2]), float(numbers[3]))
            if len(numbers) >= 4 else None
        )
        return "", bbox
    label = ""
    if isinstance(parsed, (list, tuple)):
        for item in parsed:
            if isinstance(item, str):
                label = item
                break
    elif isinstance(parsed, dict):
        label = str(parsed.get("type") or parsed.get("label") or "")
    return label, _find_bbox(parsed)


def markdown_table_rows(value: str) -> list[list[str]]:
    lines = [line.strip() for line in value.strip().splitlines() if line.strip()]
    if len(lines) < 2 or not any(_TABLE_SEPARATOR_RE.match(line) for line in lines[:3]):
        return []
    rows: list[list[str]] = []
    for line in lines:
        if _TABLE_SEPARATOR_RE.match(line):
            continue
        if "|" not in line:
            continue
        cells = [re.sub(r"\s+", " ", cell).strip() for cell in line.strip("|").split("|")]
        if any(cells):
            rows.append(cells)
    return rows


def html_table_rows(value: str) -> list[list[str]]:
    if "<table" not in value.lower():
        return []
    parser = _TableParser()
    try:
        parser.feed(value)
    except Exception:
        return []
    return parser.rows


def _classify(label: str, content: str) -> str:
    sample = f"{label} {content[:300]}".lower()
    if markdown_table_rows(content) or html_table_rows(content) or any(token in sample for token in ("table", "표")):
        return "table"
    if any(token in sample for token in ("figure", "image", "chart", "graph", "그림", "그래프", "도표", "차트")):
        return "figure"
    if any(token in sample for token in ("title", "header", "heading", "제목")) or content.lstrip().startswith("#"):
        return "title"
    if any(token in sample for token in ("formula", "equation", "수식")):
        return "formula"
    return "text"


def _content_rows(content: str) -> list[list[str]]:
    return markdown_table_rows(content) or html_table_rows(content)


def _split_untagged_markdown(text: str) -> list[tuple[str, str]]:
    parts: list[tuple[str, str]] = []
    lines = text.splitlines()
    index = 0
    buffer: list[str] = []

    def flush() -> None:
        value = "\n".join(buffer).strip()
        buffer.clear()
        if value:
            parts.append((_classify("", value), value))

    while index < len(lines):
        line = lines[index]
        if line.lstrip().startswith("#"):
            flush()
            parts.append(("title", line.strip()))
            index += 1
            continue
        if index + 1 < len(lines) and "|" in line and _TABLE_SEPARATOR_RE.match(lines[index + 1]):
            flush()
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1
            parts.append(("table", "\n".join(table_lines)))
            continue
        if not line.strip():
            flush()
        else:
            buffer.append(line)
        index += 1
    flush()
    return parts


def _official_blocks(text: str) -> tuple[list[tuple[str, str, str]], str]:
    """공식 `<|det|>type [bbox]<|/det|>content` 연속 블록을 복원한다."""
    blocks: list[tuple[str, str, str]] = []
    outside: list[str] = []
    current_label = ""
    current_detection = ""
    current_lines: list[str] | None = None

    def flush() -> None:
        nonlocal current_label, current_detection, current_lines
        if current_lines is not None:
            blocks.append((current_label, current_detection, "\n".join(current_lines).strip()))
        current_label = ""
        current_detection = ""
        current_lines = None

    for line in text.splitlines():
        match = _OFFICIAL_DET_LINE_RE.match(line)
        if match:
            flush()
            current_label = match.group(1).strip()
            current_detection = (match.group(2) or "").strip()
            trailing = match.group(3).rstrip()
            current_lines = [trailing] if trailing else []
        elif current_lines is not None:
            current_lines.append(line.rstrip())
        else:
            outside.append(line)
    flush()
    return blocks, "\n".join(outside)


def parse_uocr_text(
    text: str,
    *,
    page_number: int,
    pdf_width: float,
    pdf_height: float,
) -> list[ExperimentObject]:
    objects: list[ExperimentObject] = []
    sequences: dict[str, int] = {}

    def add(content: str, label: str = "", bbox_norm=None, raw_det: str = "") -> None:
        cleaned = content.strip()
        object_type = _classify(label, cleaned)
        if not cleaned and object_type not in {"figure", "image"}:
            return
        sequences[object_type] = sequences.get(object_type, 0) + 1
        sequence = sequences[object_type]
        rows = _content_rows(cleaned) if object_type == "table" else []
        bbox_pdf = denormalize_bbox(bbox_norm, pdf_width, pdf_height) if bbox_norm else None
        objects.append(ExperimentObject(
            object_id=f"uocr-p{page_number}-{object_type}-{sequence}",
            engine="unlimited_ocr",
            page_number=page_number,
            object_type=object_type,
            sequence=sequence,
            text="" if rows else cleaned,
            rows=rows,
            caption=cleaned.splitlines()[0][:200] if object_type == "figure" and cleaned else "",
            bbox_norm=bbox_norm,
            bbox_pdf=bbox_pdf,
            confidence="model",
            metadata={"label": label, "raw_detection": raw_det},
        ))

    consumed_spans: list[tuple[int, int]] = []
    for match in _PAIR_RE.finditer(text):
        label, bbox = parse_detection(match.group(2))
        add(match.group(1), label, bbox, match.group(2))
        consumed_spans.append(match.span())

    remainder = text
    for start, end in reversed(consumed_spans):
        remainder = remainder[:start] + "\n" + remainder[end:]
    official_blocks, remainder = _official_blocks(remainder)
    for label, detection, content in official_blocks:
        _, bbox = parse_detection(detection)
        add(content, label, bbox, detection)
    remainder = _DET_RE.sub("", remainder)
    remainder = re.sub(r"<\|/?ref\|>", "", remainder)
    for object_type, content in _split_untagged_markdown(remainder):
        add(content, object_type)
    return objects


def _page_number(path: Path) -> int | None:
    match = _PAGE_NUMBER_RE.search(path.stem)
    if match:
        return int(match.group(1))
    numbers = re.findall(r"\d+", path.stem)
    return int(numbers[-1]) if numbers else None


def parse_raw_directory(manifest_path: str | Path, raw_dir: str | Path | None = None) -> tuple[Path, list[ExperimentObject]]:
    manifest = load_manifest(manifest_path)
    run_dir = Path(manifest_path).resolve().parent
    source = Path(raw_dir).resolve() if raw_dir else run_dir / "outputs" / "unlimited_ocr" / "raw"
    page_meta = {int(item["page_number"]): item for item in manifest["pages"]}
    objects: list[ExperimentObject] = []
    for path in sorted([*source.glob("*.md"), *source.glob("*.txt")]):
        page = _page_number(path)
        if page is None or page not in page_meta:
            continue
        meta = page_meta[page]
        objects.extend(parse_uocr_text(
            path.read_text(encoding="utf-8", errors="replace"),
            page_number=page,
            pdf_width=float(meta["pdf_width"]),
            pdf_height=float(meta["pdf_height"]),
        ))
    output_path = run_dir / "outputs" / "unlimited_ocr" / "objects.jsonl"
    save_objects(output_path, objects)
    return output_path, objects
