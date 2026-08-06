"""결정론적 문서 객체 triage, 선택적 OCR 백엔드, 근거 기반 병합."""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import re
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from typing import Any, Protocol

import fitz
from PIL import Image

import config
from utils.document_objects import DocumentObject, parse_html_table, table_to_markdown
from utils.pdf_reader import PDFContent, PageContent


_VISUAL_TYPES = {"chart", "figure", "image"}
_DATA_SIGNALS = (
    "표", "그래프", "차트", "도표", "배출", "감축", "전망", "목표", "연도",
    "예산", "실적", "에너지", "자동차", "통행", "비율", "%", "tco2", "co2eq",
)
_NEGATIVE_SIGNALS = (
    "목차", "행사", "공모전", "모집", "사진", "로고", "위원회 사진", "참고 사례",
)
_MARKDOWN_SEPARATOR_RE = re.compile(
    r"^\s*\|?\s*:?-{3,}:?\s*(?:\|\s*:?-{3,}:?\s*)+\|?\s*$"
)
_PAGE_RE = re.compile(r"(?:page|p)[_-]?(\d+)", re.I)
_OFFICIAL_BLOCK_RE = re.compile(
    r"<\|det\|>([^<\n]+)<\|/det\|>(.*?)(?=<\|det\|>|\Z)",
    re.S,
)


def _normalized(value: str) -> str:
    return re.sub(r"[^0-9a-z가-힣]+", "", (value or "").casefold())


def _bbox(value: Any) -> tuple[float, float, float, float] | None:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        try:
            return tuple(float(item) for item in value)
        except (TypeError, ValueError):
            return None
    return None


def _bbox_key(value: tuple[float, float, float, float] | None) -> str:
    if value is None:
        return ""
    return "-".join(str(int(round(item / 4.0) * 4)) for item in value)


def _bbox_iou(
    left: tuple[float, float, float, float] | None,
    right: tuple[float, float, float, float] | None,
) -> float:
    if left is None or right is None:
        return 0.0
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    if intersection <= 0:
        return 0.0
    left_area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    right_area = max(0.0, right[2] - right[0]) * max(0.0, right[3] - right[1])
    union = left_area + right_area - intersection
    return intersection / union if union else 0.0


def _type_family(object_type: str) -> str:
    return "visual" if object_type in _VISUAL_TYPES else object_type


def evidence_id(obj: DocumentObject) -> str:
    """페이지·번호·좌표로 엔진에 독립적인 근거 ID를 만든다."""
    existing = str(obj.metadata.get("evidence_id") or obj.metadata.get("source_evidence_id") or "")
    if existing:
        return existing
    family = _type_family(obj.object_type)
    label = _normalized(obj.number)
    if not label and obj.caption:
        label = _normalized(obj.caption)[:48]
    anchor = label or _bbox_key(obj.bbox) or str(obj.sequence)
    digest = hashlib.sha1(
        f"p{obj.page_number}|{family}|{anchor}".encode("utf-8")
    ).hexdigest()[:12]
    return f"ev-p{obj.page_number}-{family}-{digest}"


def _cell_stats(obj: DocumentObject) -> tuple[int, int, int, float, float]:
    rows = obj.rows or []
    row_count = len(rows)
    col_count = max((len(row) for row in rows), default=0)
    total = sum(len(row) for row in rows)
    nonempty = sum(1 for row in rows for cell in row if str(cell or "").strip())
    fill_ratio = nonempty / total if total else 0.0
    ragged_ratio = (
        sum(len(row) != col_count for row in rows) / row_count
        if row_count and col_count else 1.0
    )
    return row_count, col_count, nonempty, fill_ratio, ragged_ratio


def native_confidence(obj: DocumentObject) -> tuple[float, list[str]]:
    """PyMuPDF 기본 추출만으로 충분한지 결정론적으로 평가한다."""
    reasons: list[str] = []
    if obj.object_type == "text":
        text = (obj.text or "").strip()
        if len(text) >= 80:
            return 0.98, ["native_text_sufficient"]
        if len(text) >= 20:
            return 0.78, ["native_text_short"]
        return 0.25, ["native_text_missing"]

    if obj.object_type == "table":
        rows, cols, nonempty, fill_ratio, ragged_ratio = _cell_stats(obj)
        if obj.metadata.get("missing_native") or rows < 2 or cols < 2:
            return 0.15, ["native_table_missing"]
        score = 0.96
        if fill_ratio < 0.70:
            score -= 0.30
            reasons.append(f"low_cell_fill:{fill_ratio:.2f}")
        if ragged_ratio > 0.25:
            score -= 0.25
            reasons.append(f"ragged_rows:{ragged_ratio:.2f}")
        if nonempty < 4:
            score -= 0.35
            reasons.append("too_few_cells")
        if rows >= int(getattr(config, "OCR_COMPLEX_TABLE_ROWS", 45)) or cols >= int(
            getattr(config, "OCR_COMPLEX_TABLE_COLUMNS", 12)
        ):
            score -= 0.18
            reasons.append(f"complex_table:{rows}x{cols}")
        strategy = str(obj.metadata.get("extraction_strategy") or "")
        if strategy == "text":
            score -= 0.08
            reasons.append("text_strategy_table")
        return max(0.0, min(1.0, score)), reasons or ["native_table_sufficient"]

    if obj.object_type == "chart":
        return 0.20, ["chart_requires_digitization"]
    if obj.object_type in {"figure", "image"}:
        return 0.30, ["visual_requires_classification"]
    return 0.50, ["unknown_object_type"]


@dataclass(slots=True)
class TriageDecision:
    object_id: str
    evidence_id: str
    object_type: str
    page_number: int
    bbox: tuple[float, float, float, float] | None
    native_confidence: float
    action: str
    reasons: list[str] = field(default_factory=list)
    backend: str = ""
    status: str = "planned"
    final_status: str = "needs_review"
    attempt_count: int = 0
    terminal_reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "object_id": self.object_id,
            "evidence_id": self.evidence_id,
            "object_type": self.object_type,
            "page_number": self.page_number,
            "bbox": list(self.bbox) if self.bbox else None,
            "native_confidence": round(self.native_confidence, 4),
            "action": self.action,
            "reasons": list(self.reasons),
            "backend": self.backend,
            "status": self.status,
            "final_status": self.final_status,
            "attempt_count": self.attempt_count,
            "terminal_reason": self.terminal_reason,
        }


def build_triage_plan(
    objects: list[DocumentObject],
    *,
    backend: str,
    confidence_threshold: float,
) -> list[TriageDecision]:
    decisions: list[TriageDecision] = []
    for obj in objects:
        confidence, reasons = native_confidence(obj)
        sample = obj.searchable_text().casefold()
        render_proxy = bool(obj.metadata.get("render_proxy"))
        negative = any(token in sample for token in _NEGATIVE_SIGNALS)
        data_signal = any(token in sample for token in _DATA_SIGNALS)

        if render_proxy:
            action = "transport_only"
            reasons = [*reasons, "page_render_transport"]
        elif obj.object_type == "text":
            action = "native_keep"
        elif negative and obj.object_type != "text":
            action = "skip_non_data"
            reasons = [*reasons, "decorative_or_reference"]
        elif obj.object_type == "table":
            action = "ocr_required" if confidence < confidence_threshold else "native_keep"
        elif obj.object_type == "chart":
            action = "ocr_required"
        elif obj.object_type in {"figure", "image"}:
            action = "ocr_required" if data_signal else "skip_non_data"
            if not data_signal:
                reasons = [*reasons, "no_data_signal"]
        else:
            action = "native_keep"

        if action == "native_keep":
            final_status = "extracted"
            terminal_reason = "PyMuPDF 기본 추출 사용"
        elif action in {"skip_non_data", "transport_only"}:
            final_status = "not_relevant"
            terminal_reason = "비데이터 객체 또는 렌더 전송 프록시"
        else:
            final_status = "needs_review"
            terminal_reason = "OCR/VLM 판독 대기"

        decisions.append(TriageDecision(
            object_id=obj.object_id,
            evidence_id=evidence_id(obj),
            object_type=obj.object_type,
            page_number=obj.page_number,
            bbox=obj.bbox,
            native_confidence=confidence,
            action=action,
            reasons=reasons,
            backend=backend if action == "ocr_required" else "",
            final_status=final_status,
            terminal_reason=terminal_reason,
        ))
    return decisions


def apply_triage_metadata(
    objects: list[DocumentObject],
    decisions: list[TriageDecision] | list[dict[str, Any]],
) -> list[DocumentObject]:
    by_id: dict[str, dict[str, Any]] = {}
    by_evidence: dict[str, dict[str, Any]] = {}
    for row in decisions:
        value = row.to_dict() if isinstance(row, TriageDecision) else row
        if isinstance(value, dict) and value.get("object_id"):
            by_id[str(value["object_id"])] = value
            evidence = str(value.get("evidence_id") or "").strip()
            if evidence:
                by_evidence[evidence] = value
    for obj in objects:
        object_evidence = evidence_id(obj)
        row = by_id.get(obj.object_id) or by_evidence.get(object_evidence, {})
        obj.metadata["evidence_id"] = str(row.get("evidence_id") or object_evidence)
        if row:
            obj.metadata["native_confidence"] = row.get("native_confidence")
            obj.metadata["triage_action"] = row.get("action")
            obj.metadata["triage_reasons"] = row.get("reasons", [])
            obj.metadata["ocr_backend"] = row.get("backend", "")
            obj.metadata["ocr_status"] = row.get("status", "planned")
            obj.metadata["final_status"] = row.get("final_status", "needs_review")
            obj.metadata["attempt_count"] = int(row.get("attempt_count", 0) or 0)
            obj.metadata["terminal_reason"] = row.get("terminal_reason", "")
    return objects


def _compatible(left: DocumentObject, right: DocumentObject) -> bool:
    return left.object_type == right.object_type or {
        left.object_type, right.object_type
    } <= _VISUAL_TYPES


def _similarity(left: DocumentObject, right: DocumentObject) -> float:
    if left.page_number != right.page_number or not _compatible(left, right):
        return 0.0
    if left.number and right.number and _normalized(left.number) == _normalized(right.number):
        return 1.0
    text_left = _normalized(left.searchable_text())[:5000]
    text_right = _normalized(right.searchable_text())[:5000]
    text_score = SequenceMatcher(None, text_left, text_right).ratio() if text_left and text_right else 0.0
    iou = _bbox_iou(left.bbox, right.bbox)
    return max(text_score, iou, 0.7 * text_score + 0.3 * iou)


def _nonempty_cells(obj: DocumentObject) -> int:
    return sum(1 for row in obj.rows for cell in row if str(cell or "").strip())


def merge_document_objects(
    native_objects: list[DocumentObject],
    ocr_objects: list[DocumentObject],
) -> list[DocumentObject]:
    """원본 근거 ID를 유지하면서 OCR을 누락·저신뢰 객체에만 반영한다."""
    native_by_evidence: dict[str, DocumentObject] = {}
    for obj in native_objects:
        eid = evidence_id(obj)
        obj.metadata["evidence_id"] = eid
        current = native_by_evidence.get(eid)
        if current is None or _nonempty_cells(obj) > _nonempty_cells(current):
            native_by_evidence[eid] = obj

    additions: list[DocumentObject] = []
    for candidate in ocr_objects:
        eid = evidence_id(candidate)
        matched = native_by_evidence.get(eid)
        if matched is None:
            candidates = [obj for obj in native_by_evidence.values() if _compatible(obj, candidate)]
            ranked = sorted(
                ((_similarity(obj, candidate), obj) for obj in candidates),
                key=lambda pair: pair[0],
                reverse=True,
            )
            if ranked and ranked[0][0] >= 0.55:
                matched = ranked[0][1]
                eid = evidence_id(matched)
        candidate.metadata["evidence_id"] = eid
        backend = str(candidate.metadata.get("engine") or candidate.metadata.get("ocr_backend") or "ocr")

        if matched is None:
            candidate.metadata.update({
                "ocr_backend": backend,
                "ocr_status": "added_missing_object",
                "source_object_ids": [candidate.object_id],
            })
            additions.append(candidate)
            native_by_evidence[eid] = candidate
            continue

        native_cells = _nonempty_cells(matched)
        candidate_cells = _nonempty_cells(candidate)
        native_score = float(matched.metadata.get("native_confidence", native_confidence(matched)[0]) or 0.0)
        should_replace_table = (
            matched.object_type == "table"
            and candidate_cells >= max(native_cells + 2, int(native_cells * 1.10))
            and native_score < float(getattr(config, "OCR_NATIVE_CONFIDENCE_THRESHOLD", 0.78))
        )
        merged = replace(matched)
        merged.metadata = dict(matched.metadata)
        if should_replace_table:
            merged.rows = candidate.rows
            merged.text = candidate.text or table_to_markdown(candidate.rows, max_rows=max(2, len(candidate.rows)))
            merged.bbox = candidate.bbox or matched.bbox
            status = "replaced_low_confidence_native"
        else:
            if not merged.text and candidate.text:
                merged.text = candidate.text
            if not merged.rows and candidate.rows:
                merged.rows = candidate.rows
            if not merged.caption and candidate.caption:
                merged.caption = candidate.caption
            if not merged.bbox and candidate.bbox:
                merged.bbox = candidate.bbox
            status = "enriched_native" if any((candidate.text, candidate.rows, candidate.caption)) else "duplicate_skipped"
        merged.metadata.update({
            "evidence_id": eid,
            "ocr_backend": backend,
            "ocr_status": status,
            "source_object_ids": [matched.object_id, candidate.object_id],
            "ocr_cell_count": candidate_cells,
        })
        native_by_evidence[eid] = merged

    merged_objects = list(native_by_evidence.values())
    for obj in additions:
        if obj not in merged_objects:
            merged_objects.append(obj)
    merged_objects.sort(key=lambda obj: (obj.page_number, obj.sequence, obj.object_type, obj.object_id))
    return merged_objects


def _markdown_table_rows(value: str) -> list[list[str]]:
    lines = [line.strip() for line in value.splitlines() if line.strip()]
    if len(lines) < 2 or not any(_MARKDOWN_SEPARATOR_RE.match(line) for line in lines[:3]):
        return []
    rows: list[list[str]] = []
    for line in lines:
        if _MARKDOWN_SEPARATOR_RE.match(line) or "|" not in line:
            continue
        row = [re.sub(r"\s+", " ", cell).strip() for cell in line.strip("|").split("|")]
        if any(row):
            rows.append(row)
    return rows


def _detection_bbox(value: str, width: float, height: float) -> tuple[float, float, float, float] | None:
    try:
        parsed = ast.literal_eval(value.strip())
    except (ValueError, SyntaxError):
        parsed = None
    numbers: list[float] = []

    def collect(item: Any) -> None:
        if isinstance(item, (list, tuple)) and len(item) == 4 and all(
            isinstance(number, (int, float)) for number in item
        ):
            numbers.extend(float(number) for number in item)
        elif isinstance(item, (list, tuple)):
            for nested in item:
                collect(nested)
        elif isinstance(item, dict):
            for key in ("bbox", "box", "coordinate", "coordinates"):
                if key in item:
                    collect(item[key])

    collect(parsed)
    if len(numbers) < 4:
        raw = re.findall(r"-?\d+(?:\.\d+)?", value)
        numbers = [float(number) for number in raw[:4]]
    if len(numbers) < 4:
        return None
    x0, y0, x1, y1 = numbers[:4]
    if max(abs(number) for number in (x0, y0, x1, y1)) <= 1000 and width and height:
        x0, x1 = x0 * width / 1000.0, x1 * width / 1000.0
        y0, y1 = y0 * height / 1000.0, y1 * height / 1000.0
    return x0, y0, x1, y1


def _ocr_object_type(label: str, content: str) -> str:
    sample = f"{label} {content[:300]}".casefold()
    if _markdown_table_rows(content) or "<table" in sample or any(token in sample for token in ("table", "표")):
        return "table"
    if any(token in sample for token in ("chart", "graph", "그래프", "차트", "도표")):
        return "chart"
    if any(token in sample for token in ("figure", "image", "그림", "사진")):
        return "image"
    return "text"


def parse_ocr_text(
    text: str,
    *,
    page_number: int,
    page_width: float,
    page_height: float,
    backend: str,
) -> list[DocumentObject]:
    """Unlimited-OCR 계열 태그 또는 일반 Markdown/HTML을 DocumentObject로 변환한다."""
    blocks: list[tuple[str, str, str]] = []
    spans: list[tuple[int, int]] = []
    for match in _OFFICIAL_BLOCK_RE.finditer(text):
        detection = match.group(1).strip()
        content = match.group(2).strip()
        label = detection.split(maxsplit=1)[0] if detection else ""
        blocks.append((label, detection, content))
        spans.append(match.span())
    remainder = text
    for start, end in reversed(spans):
        remainder = remainder[:start] + "\n" + remainder[end:]

    html_tables = re.findall(r"<table\b.*?</table>", remainder, re.I | re.S)
    for table in html_tables:
        blocks.append(("table", "", table))
        remainder = remainder.replace(table, "\n", 1)
    lines = remainder.splitlines()
    index = 0
    buffer: list[str] = []

    def flush_buffer() -> None:
        value = "\n".join(buffer).strip()
        buffer.clear()
        if value:
            blocks.append(("text", "", value))

    while index < len(lines):
        line = lines[index]
        if index + 1 < len(lines) and "|" in line and _MARKDOWN_SEPARATOR_RE.match(lines[index + 1]):
            flush_buffer()
            table_lines = [line, lines[index + 1]]
            index += 2
            while index < len(lines) and "|" in lines[index]:
                table_lines.append(lines[index])
                index += 1
            blocks.append(("table", "", "\n".join(table_lines)))
            continue
        if line.strip():
            buffer.append(line)
        else:
            flush_buffer()
        index += 1
    flush_buffer()

    objects: list[DocumentObject] = []
    counts: dict[str, int] = {}
    for label, detection, content in blocks:
        object_type = _ocr_object_type(label, content)
        rows = _markdown_table_rows(content)
        if not rows and "<table" in content.casefold():
            rows = parse_html_table(content)
        cleaned = re.sub(r"<[^>]+>", " ", unescape(content)) if rows else content.strip()
        if not rows and not cleaned:
            continue
        counts[object_type] = counts.get(object_type, 0) + 1
        sequence = counts[object_type]
        bbox = _detection_bbox(detection, page_width, page_height) if detection else None
        objects.append(DocumentObject(
            object_id=f"ocr-p{page_number}-{object_type}-{sequence}",
            object_type=object_type,
            page_number=page_number,
            sequence=sequence,
            text="" if rows else cleaned,
            rows=rows,
            caption=(cleaned.splitlines()[0][:200] if object_type in _VISUAL_TYPES and cleaned else ""),
            bbox=bbox,
            metadata={
                "engine": backend,
                "ocr_backend": backend,
                "ocr_status": "parsed",
                "raw_detection": detection,
            },
        ))
    return objects


def vision_analyses_to_objects(analyses: list[dict[str, Any]]) -> list[DocumentObject]:
    objects: list[DocumentObject] = []
    per_page: dict[int, int] = {}
    for analysis in analyses:
        if not isinstance(analysis, dict):
            continue
        try:
            page_number = int(analysis.get("page_number"))
        except (TypeError, ValueError):
            continue
        raw_rows = analysis.get("table") if isinstance(analysis.get("table"), list) else []
        rows: list[list[str]] = []
        if raw_rows:
            headers = ["연도", "항목", "종류", "값", "단위", "fields"]
            rows.append(headers)
            for row in raw_rows:
                if not isinstance(row, dict):
                    continue
                rows.append([
                    str(row.get("연도") or ""),
                    str(row.get("항목") or ""),
                    str(row.get("종류") or ""),
                    str(row.get("값") if row.get("값") is not None else ""),
                    str(row.get("단위") or analysis.get("unit") or ""),
                    json.dumps(row.get("fields", {}), ensure_ascii=False, sort_keys=True),
                ])
        if len(rows) < 2:
            continue
        per_page[page_number] = per_page.get(page_number, 0) + 1
        sequence = per_page[page_number]
        evidence_ids = analysis.get("source_evidence_ids")
        source_evidence_values = [str(value) for value in evidence_ids] if isinstance(evidence_ids, list) else []
        source_evidence = source_evidence_values[0] if len(source_evidence_values) == 1 else ""
        objects.append(DocumentObject(
            object_id=f"vlm-p{page_number}-chart-{sequence}",
            object_type="table" if str(analysis.get("chart_type")) == "표" else "chart",
            page_number=page_number,
            sequence=sequence,
            text=table_to_markdown(rows, max_rows=max(2, len(rows))),
            rows=rows,
            caption=str(analysis.get("title") or ""),
            bbox=_bbox(analysis.get("source_bbox")),
            metadata={
                "engine": "vlm",
                "ocr_backend": "vlm",
                "ocr_status": "parsed",
                "source_evidence_id": source_evidence,
                "source_evidence_ids": source_evidence_values,
                "confidence": analysis.get("confidence", ""),
                "target_sheet": analysis.get("target_sheet", ""),
            },
        ))
    return objects


class OCRBackend(Protocol):
    name: str
    uses_vision: bool

    def load(self, document: PDFContent, candidates: list[TriageDecision]) -> list[DocumentObject]: ...


@dataclass(slots=True)
class VisionOCRBackend:
    name: str = "vlm"
    uses_vision: bool = True

    def load(self, document: PDFContent, candidates: list[TriageDecision]) -> list[DocumentObject]:
        return []


@dataclass(slots=True)
class DisabledOCRBackend:
    name: str = "none"
    uses_vision: bool = False

    def load(self, document: PDFContent, candidates: list[TriageDecision]) -> list[DocumentObject]:
        return []


@dataclass(slots=True)
class DirectoryOCRBackend:
    root: Path
    name: str = "unlimited_ocr"
    uses_vision: bool = False

    def load(self, document: PDFContent, candidates: list[TriageDecision]) -> list[DocumentObject]:
        if not self.root.exists():
            raise FileNotFoundError(f"OCR 결과 디렉터리를 찾을 수 없습니다: {self.root}")
        candidate_pages = {row.page_number for row in candidates if row.action == "ocr_required"}
        pages = {page.page_number: page for page in document.pages}
        objects: list[DocumentObject] = []

        jsonl_files = sorted(self.root.rglob("project_document_objects.jsonl"))
        if not jsonl_files:
            jsonl_files = sorted(self.root.glob("*.jsonl"))
        for path in jsonl_files:
            for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(row, dict):
                    continue
                try:
                    obj = DocumentObject.from_dict(row)
                except (TypeError, ValueError):
                    continue
                if candidate_pages and obj.page_number not in candidate_pages:
                    continue
                obj.metadata.update({"engine": self.name, "ocr_backend": self.name, "ocr_status": "loaded"})
                objects.append(obj)
        loaded_pages = {obj.page_number for obj in objects}
        for path in sorted([*self.root.rglob("*.md"), *self.root.rglob("*.txt")]):
            match = _PAGE_RE.search(path.stem)
            if not match:
                numbers = re.findall(r"\d+", path.stem)
                page_number = int(numbers[-1]) if numbers else 0
            else:
                page_number = int(match.group(1))
            if (
                not page_number
                or page_number in loaded_pages
                or (candidate_pages and page_number not in candidate_pages)
            ):
                continue
            page = pages.get(page_number)
            objects.extend(parse_ocr_text(
                path.read_text(encoding="utf-8", errors="replace"),
                page_number=page_number,
                page_width=float(page.width if page else 0.0),
                page_height=float(page.height if page else 0.0),
                backend=self.name,
            ))
        return objects


def get_ocr_backend(name: str, results_dir: str | Path | None = None) -> OCRBackend:
    normalized = (name or "vlm").strip().casefold().replace("-", "_")
    if normalized in {"none", "off", "disabled"}:
        return DisabledOCRBackend()
    if normalized in {"unlimited_ocr", "uocr", "markdown", "directory"}:
        if not results_dir:
            raise ValueError("Unlimited-OCR 백엔드는 OCR_RESULTS_DIR 또는 --ocr-results-dir가 필요합니다")
        return DirectoryOCRBackend(Path(results_dir).expanduser().resolve())
    if normalized in {"vlm", "vision", "gemini", "codex"}:
        return VisionOCRBackend()
    raise ValueError(f"지원하지 않는 OCR 백엔드입니다: {name}")


def _candidate_rect(obj: DocumentObject, page: PageContent) -> fitz.Rect:
    # 캡션만 검출된 표·차트는 실제 객체 범위를 알 수 없으므로 페이지 전체를 보낸다.
    if obj.bbox and not obj.metadata.get("caption_only") and not obj.metadata.get("missing_native"):
        rect = fitz.Rect(obj.bbox)
        rect.x0 = max(0.0, rect.x0 - 8)
        rect.y0 = max(0.0, rect.y0 - 8)
        rect.x1 = min(float(page.width or rect.x1), rect.x1 + 8)
        rect.y1 = min(float(page.height or rect.y1), rect.y1 + 8)
        if rect.width >= 80 and rect.height >= 40:
            return rect
    return fitz.Rect(0.0, 0.0, float(page.width), float(page.height))


def render_ocr_candidate_images(
    document: PDFContent,
    objects: list[DocumentObject],
    decisions: list[TriageDecision],
) -> list[tuple[PageContent, dict[str, Any]]]:
    """기존 이미지가 없는 저신뢰 객체만 PDF에서 지연 렌더링한다."""
    if not document.source_path:
        return []
    page_map = {page.page_number: page for page in document.pages}
    object_map = {obj.object_id: obj for obj in objects}
    required = [row for row in decisions if row.action == "ocr_required"]
    seen: set[str] = set()
    rendered: list[tuple[PageContent, dict[str, Any]]] = []
    try:
        pdf = fitz.open(document.source_path)
    except Exception:
        return []
    try:
        for row in required:
            if row.evidence_id in seen:
                continue
            page = page_map.get(row.page_number)
            obj = object_map.get(row.object_id)
            if page is None or obj is None or row.page_number < 1 or row.page_number > len(pdf):
                continue
            rect = _candidate_rect(obj, page)
            matrix = fitz.Matrix(
                float(getattr(config, "OCR_RENDER_DPI", 200)) / 72.0,
                float(getattr(config, "OCR_RENDER_DPI", 200)) / 72.0,
            )
            pix = pdf[row.page_number - 1].get_pixmap(matrix=matrix, clip=rect, alpha=False)
            image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
            max_size = int(getattr(config, "MAX_IMAGE_SIZE", 1568))
            if max(image.size) > max_size:
                ratio = max_size / max(image.size)
                image = image.resize(
                    (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
                    Image.LANCZOS,
                )
            buffer = io.BytesIO()
            image.save(buffer, format="PNG")
            rendered.append((page, {
                "base64": base64.b64encode(buffer.getvalue()).decode("ascii"),
                "width": image.width,
                "height": image.height,
                "caption": obj.caption or f"p{row.page_number} {obj.object_type} OCR candidate",
                "bbox": list(rect),
                "source_kind": "ocr_candidate",
                "source_object_ids": [row.object_id],
                "source_evidence_ids": [row.evidence_id],
                "ocr_required": True,
            }))
            seen.add(row.evidence_id)
    finally:
        pdf.close()
    return rendered
