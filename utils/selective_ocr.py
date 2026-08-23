"""결정론적 문서 객체 triage, 선택적 OCR 백엔드, 근거 기반 병합."""

from __future__ import annotations

import ast
import base64
import hashlib
import io
import json
import re
import time
from dataclasses import dataclass, field, replace
from difflib import SequenceMatcher
from html import unescape
from pathlib import Path
from typing import Any, Protocol

import fitz
from PIL import Image

import config
from utils.document_objects import DocumentObject, parse_html_table, table_to_markdown
from utils.object_routing import deduplicate_evidence_objects
from utils.pdf_reader import PDFContent, PageContent
from utils.physical_objects import (
    PhysicalObjectIdentity,
    aggregate_identity_metadata,
    aliases_from_metadata,
    identity_from_document_object,
    match_physical_objects,
    normalize_object_ids,
    physical_object_id,
    same_physical_object,
)


_VISUAL_TYPES = {"chart", "figure", "image"}
_DATA_SIGNALS = (
    "표", "그래프", "차트", "도표", "분포", "구성", "비중", "비율", "추이",
    "변화", "비교", "현황", "지표", "통계", "배출", "감축", "전망", "목표",
    "연도", "예산", "실적", "에너지", "자동차", "통행", "면적", "인구",
    "기온", "강수", "위험", "취약", "피해", "용량", "시설", "인프라",
    "분야별", "종류별", "단계별", "추진계획", "추진체계", "전략체계",
    "로드맵", "흐름도", "절차", "업무", "정의", "%", "tco2", "co2eq",
)
_NEGATIVE_SIGNALS = (
    "목차", "행사", "공모전", "모집", "사진", "로고", "위원회 사진", "참고 사례",
)
_CAPTION_NUMBER_RE = re.compile(
    r"(?:\[?\s*)?(?:표|그림|figure|fig\.?)\s*"
    r"[0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:\s*[-–—.]\s*\d+)?",
    re.IGNORECASE,
)
_YEAR_OR_PERIOD_RE = re.compile(
    r"(?:19|20)\d{2}\s*(?:년)?(?:\s*[~～\-–—]\s*(?:19|20)?\d{2}\s*년?)?"
)
_NUMBER_RE = re.compile(r"(?<![0-9a-z가-힣])[-+]?\d+(?:,\d{3})*(?:\.\d+)?")
_QUANTIFIED_UNIT_RE = re.compile(
    r"[-+]?\d+(?:,\d{3})*(?:\.\d+)?\s*"
    r"(?:%|％|℃|°\s*c|mm|cm|km|m2|m²|㎡|km2|km²|㎢|"
    r"tco2(?:eq)?|ktco2(?:eq)?|co2eq|kwh|mwh|gwh|toe|"
    r"명|개|대|건|곳|개소|억원|백만원|천만원|원)",
    re.IGNORECASE,
)
_STRONG_VISUAL_METADATA = (
    "has_axis", "axis_detected", "has_legend", "legend_detected",
    "line_grid_density", "vector_chart", "multi_panel", "is_multi_panel",
    "chart_like", "table_like", "flow_like", "diagram_like",
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


def _positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


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


def _metadata_enabled(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value > 0
    if isinstance(value, (list, tuple, set, dict)):
        return bool(value)
    return str(value or "").strip().casefold() not in {"", "0", "false", "none", "null"}


def _metadata_int(value: Any) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def strong_data_signals(obj: DocumentObject) -> list[str]:
    """OCR/VLM 사전 제외를 막을 만큼 강한 결정론적 데이터 신호를 반환한다."""
    cells = " ".join(str(cell or "") for row in obj.rows for cell in row)
    local_text = " ".join(
        part for part in (obj.number, obj.caption, obj.text, cells) if part
    ).casefold()
    context_text = str(obj.nearby_text or "").casefold()
    without_caption_number = _CAPTION_NUMBER_RE.sub(" ", local_text)

    captioned = bool(obj.number or _CAPTION_NUMBER_RE.search(obj.caption or ""))
    local_data_terms = sorted({token for token in _DATA_SIGNALS if token in local_text})
    context_data_terms = sorted({token for token in _DATA_SIGNALS if token in context_text})
    has_year_or_period = bool(_YEAR_OR_PERIOD_RE.search(without_caption_number))
    has_quantified_unit = bool(_QUANTIFIED_UNIT_RE.search(without_caption_number))
    has_numeric_value = bool(_NUMBER_RE.search(without_caption_number))

    visual_metadata: list[str] = []
    for key in _STRONG_VISUAL_METADATA:
        if _metadata_enabled(obj.metadata.get(key)):
            visual_metadata.append(key)
    drawing_count = _metadata_int(obj.metadata.get("drawing_count"))
    if drawing_count >= 20:
        visual_metadata.append("vector_density")
    panel_count = _metadata_int(obj.metadata.get("panel_count"))
    if panel_count >= 2:
        visual_metadata.append("multi_panel")

    is_structured = obj.object_type in {"table", "chart"}
    is_visual = obj.object_type in _VISUAL_TYPES
    quantitative = has_year_or_period or has_quantified_unit or has_numeric_value
    semantic = bool(local_data_terms)
    contextual = bool(context_data_terms) and bool(
        captioned or obj.metadata.get("caption_only") or visual_metadata
    )
    qualified = bool(
        is_structured
        or (
            is_visual
            and (
                visual_metadata
                or (captioned and (quantitative or semantic))
                or semantic
                or contextual
            )
        )
    )
    if not qualified:
        return []

    reasons: list[str] = []
    if obj.object_type == "table":
        reasons.append("strong:table_structure")
    elif obj.object_type == "chart":
        reasons.append("strong:chart_structure")
    if captioned:
        reasons.append("strong:numbered_caption")
    if has_year_or_period:
        reasons.append("strong:year_or_period")
    if has_quantified_unit:
        reasons.append("strong:quantified_unit")
    elif has_numeric_value:
        reasons.append("strong:numeric_value")
    if local_data_terms:
        reasons.append(f"strong:data_terms:{','.join(local_data_terms[:5])}")
    elif contextual:
        reasons.append(f"strong:context_terms:{','.join(context_data_terms[:5])}")
    if visual_metadata:
        reasons.append(f"strong:visual_features:{','.join(sorted(set(visual_metadata)))}")
    return reasons


@dataclass(slots=True)
class TriageDecision:
    object_id: str
    evidence_id: str
    object_type: str
    page_number: int
    bbox: tuple[float, float, float, float] | None
    native_confidence: float
    action: str
    caption: str = ""
    number: str = ""
    caption_only: bool = False
    render_proxy: bool = False
    missing_native: bool = False
    engine: str = ""
    canonical_object_id: str = ""
    physical_object_id: str = ""
    alias_object_ids: list[str] = field(default_factory=list)
    source_object_ids: list[str] = field(default_factory=list)
    render_group_id: str = ""
    render_variant: str = ""
    panel_index: int | None = None
    panel_count: int = 0
    context_only: bool = False
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
            "caption": self.caption,
            "number": self.number,
            "caption_only": self.caption_only,
            "render_proxy": self.render_proxy,
            "missing_native": self.missing_native,
            "engine": self.engine,
            "canonical_object_id": self.canonical_object_id,
            "physical_object_id": self.physical_object_id,
            "alias_object_ids": list(self.alias_object_ids),
            "source_object_ids": list(self.source_object_ids),
            "render_group_id": self.render_group_id,
            "render_variant": self.render_variant,
            "panel_index": self.panel_index,
            "panel_count": self.panel_count,
            "context_only": self.context_only,
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
        negative_signals = sorted({token for token in _NEGATIVE_SIGNALS if token in sample})
        strong_signals = strong_data_signals(obj)
        reasons = [*reasons, *strong_signals]

        if render_proxy:
            action = "transport_only"
            reasons = [*reasons, "page_render_transport"]
        elif obj.object_type == "text":
            action = "native_keep"
        elif obj.object_type == "table":
            action = "ocr_required" if confidence < confidence_threshold else "native_keep"
        elif obj.object_type == "chart":
            action = "ocr_required"
        elif obj.object_type in {"figure", "image"}:
            action = "ocr_required" if strong_signals else "skip_non_data"
            if not strong_signals:
                reasons = [*reasons, "no_strong_data_signal"]
        elif negative_signals:
            action = "skip_non_data"
        else:
            action = "native_keep"

        if negative_signals:
            reasons.append(f"negative:{','.join(negative_signals)}")
            if action in {"ocr_required", "native_keep"} and obj.object_type != "text":
                reasons.append("negative_overridden_by_strong_or_structured_signal")
            elif action == "skip_non_data":
                reasons.append("decorative_or_reference")

        if action == "native_keep":
            final_status = "extracted"
            terminal_reason = "PyMuPDF 기본 추출 사용"
        elif action in {"skip_non_data", "transport_only"}:
            final_status = "not_relevant"
            terminal_reason = "비데이터 객체 또는 렌더 전송 프록시"
        else:
            final_status = "needs_review"
            terminal_reason = "OCR/VLM 판독 대기"

        identity = identity_from_document_object(obj)
        canonical_object_id = str(
            obj.metadata.get("canonical_object_id") or obj.object_id
        ).strip()
        aliases = [
            value
            for value in aliases_from_metadata(obj.metadata, obj.object_id)
            if value != canonical_object_id
        ]
        source_object_ids = list(normalize_object_ids(
            obj.object_id,
            obj.metadata.get("source_object_ids"),
        ))
        decisions.append(TriageDecision(
            object_id=obj.object_id,
            evidence_id=evidence_id(obj),
            object_type=obj.object_type,
            page_number=obj.page_number,
            bbox=obj.bbox,
            native_confidence=confidence,
            action=action,
            caption=obj.caption,
            number=obj.number,
            caption_only=bool(obj.metadata.get("caption_only")),
            render_proxy=render_proxy,
            missing_native=bool(obj.metadata.get("missing_native")),
            engine=str(obj.metadata.get("engine") or "").strip(),
            canonical_object_id=canonical_object_id,
            physical_object_id=str(obj.metadata.get("physical_object_id") or "").strip()
            or physical_object_id(identity),
            alias_object_ids=aliases,
            source_object_ids=source_object_ids,
            render_group_id=str(obj.metadata.get("render_group_id") or "").strip(),
            render_variant=str(obj.metadata.get("render_variant") or "").strip(),
            panel_index=_positive_int(obj.metadata.get("panel_index")),
            panel_count=_positive_int(obj.metadata.get("panel_count")) or 0,
            context_only=bool(obj.metadata.get("context_only")),
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
    by_evidence: dict[str, list[dict[str, Any]]] = {}
    for row in decisions:
        value = row.to_dict() if isinstance(row, TriageDecision) else row
        if isinstance(value, dict) and value.get("object_id"):
            by_id[str(value["object_id"])] = value
            evidence = str(value.get("evidence_id") or "").strip()
            if evidence:
                by_evidence.setdefault(evidence, []).append(value)
    for obj in objects:
        object_evidence = evidence_id(obj)
        object_ids = normalize_object_ids(
            obj.object_id,
            obj.metadata.get("canonical_object_id"),
            obj.metadata.get("alias_object_ids"),
            obj.metadata.get("duplicate_object_ids"),
            obj.metadata.get("source_object_ids"),
        )
        row = next((by_id[value] for value in object_ids if value in by_id), None)
        if row is None:
            evidence_rows = by_evidence.get(object_evidence, [])
            row = evidence_rows[0] if len(evidence_rows) == 1 else None
        row = row or {}
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
            obj.metadata["canonical_object_id"] = str(
                row.get("canonical_object_id") or obj.metadata.get("canonical_object_id") or obj.object_id
            )
            obj.metadata["physical_object_id"] = str(
                row.get("physical_object_id") or obj.metadata.get("physical_object_id") or ""
            )
            obj.metadata["alias_object_ids"] = list(normalize_object_ids(
                obj.metadata.get("alias_object_ids"),
                row.get("alias_object_ids"),
            ))
            obj.metadata["source_object_ids"] = list(normalize_object_ids(
                obj.metadata.get("source_object_ids"),
                row.get("source_object_ids"),
                obj.object_id,
            ))
            obj.metadata["render_group_id"] = str(
                row.get("render_group_id") or obj.metadata.get("render_group_id") or ""
            )
            obj.metadata["render_variant"] = str(
                row.get("render_variant") or obj.metadata.get("render_variant") or ""
            )
            if row.get("panel_index") is not None:
                obj.metadata["panel_index"] = row.get("panel_index")
            if row.get("panel_count"):
                obj.metadata["panel_count"] = row.get("panel_count")
            if row.get("context_only"):
                obj.metadata["context_only"] = True
            if row.get("caption_only"):
                obj.metadata["had_caption_proxy"] = True
            if row.get("render_proxy"):
                obj.metadata["had_render_proxy"] = True
            if row.get("missing_native"):
                obj.metadata["had_missing_native_proxy"] = True
    return objects


def _triage_identity(decision: TriageDecision) -> PhysicalObjectIdentity:
    return PhysicalObjectIdentity(
        object_id=decision.object_id,
        page_number=decision.page_number,
        object_type=decision.object_type,
        evidence_id=decision.evidence_id,
        bbox=decision.bbox,
        caption=decision.caption,
        number=decision.number,
        engine=decision.engine or decision.backend,
        caption_only=decision.caption_only,
        render_proxy=decision.render_proxy,
        missing_native=decision.missing_native,
        canonical_object_id=decision.canonical_object_id,
        physical_object_id=decision.physical_object_id,
        alias_object_ids=tuple(decision.alias_object_ids),
        render_group_id=decision.render_group_id,
        render_variant=decision.render_variant,
        panel_index=decision.panel_index,
        panel_count=decision.panel_count,
        context_only=decision.context_only,
    )


def reconcile_triage_decisions(
    objects: list[DocumentObject],
    decisions: list[TriageDecision],
) -> list[TriageDecision]:
    """중복 제거 후 Triage 행을 단일 대표 객체와 다시 연결한다."""
    object_identities = [
        (obj, identity_from_document_object(obj))
        for obj in objects
    ]
    for decision in decisions:
        identity = _triage_identity(decision)
        matches = [
            obj
            for obj, object_identity in object_identities
            if same_physical_object(identity, object_identity)
        ]
        if len(matches) != 1:
            continue
        matched = matches[0]
        decision.canonical_object_id = str(
            matched.metadata.get("canonical_object_id") or matched.object_id
        )
        decision.physical_object_id = str(
            matched.metadata.get("physical_object_id") or ""
        ) or physical_object_id(identity_from_document_object(matched))
        decision.alias_object_ids = list(normalize_object_ids(
            decision.alias_object_ids,
            matched.metadata.get("alias_object_ids"),
            matched.metadata.get("duplicate_object_ids"),
            decision.object_id if decision.object_id != decision.canonical_object_id else "",
        ))
        decision.source_object_ids = list(normalize_object_ids(
            decision.source_object_ids,
            matched.metadata.get("source_object_ids"),
            decision.object_id,
            matched.object_id,
        ))
    return decisions


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
    for obj in native_objects:
        eid = evidence_id(obj)
        obj.metadata["evidence_id"] = eid
        obj.metadata.setdefault("canonical_object_id", obj.object_id)
        obj.metadata.setdefault(
            "physical_object_id", physical_object_id(identity_from_document_object(obj))
        )

    # 동일 evidence_id만으로 객체를 버리지 않는다. 프록시·캡션·번호·좌표 등
    # 공통 물리 객체 규칙으로 확인된 표현만 먼저 통합한다.
    merged_objects, _removed = deduplicate_evidence_objects(native_objects)

    def by_evidence(value: str) -> list[DocumentObject]:
        return [obj for obj in merged_objects if evidence_id(obj) == value]

    def known_ids(obj: DocumentObject) -> set[str]:
        return set(normalize_object_ids(
            obj.object_id,
            obj.metadata.get("canonical_object_id"),
            obj.metadata.get("alias_object_ids"),
            obj.metadata.get("duplicate_object_ids"),
        ))

    def replace_object(previous: DocumentObject, current: DocumentObject) -> None:
        for index, obj in enumerate(merged_objects):
            if obj is previous or obj.object_id == previous.object_id:
                merged_objects[index] = current
                return
        merged_objects.append(current)

    for candidate in ocr_objects:
        eid = evidence_id(candidate)
        candidate.metadata["evidence_id"] = eid
        explicit_source_ids = set(normalize_object_ids(
            candidate.metadata.get("source_object_ids"),
            candidate.metadata.get("canonical_object_id"),
        ))
        evidence_candidates = by_evidence(eid)
        explicit_matches = [
            obj for obj in evidence_candidates
            if explicit_source_ids and explicit_source_ids & known_ids(obj)
        ]
        physical_matches = [
            obj for obj in evidence_candidates
            if same_physical_object(
                identity_from_document_object(obj),
                identity_from_document_object(candidate),
            )
        ]
        matched = explicit_matches[0] if len(explicit_matches) == 1 else None
        if matched is None and len(physical_matches) == 1:
            matched = physical_matches[0]
        if (
            matched is None
            and len(evidence_candidates) == 1
            and candidate.metadata.get("source_evidence_id")
        ):
            # 단일 source_evidence_id는 VLM/OCR 입력 객체가 명시한 직접 연결이다.
            matched = evidence_candidates[0]
        if matched is None:
            candidates = [obj for obj in merged_objects if _compatible(obj, candidate)]
            ranked = sorted(
                ((_similarity(obj, candidate), obj) for obj in candidates),
                key=lambda pair: pair[0],
                reverse=True,
            )
            if (
                ranked
                and ranked[0][0] >= 0.55
                and (len(ranked) == 1 or ranked[0][0] - ranked[1][0] >= 0.08)
            ):
                matched = ranked[0][1]
                eid = evidence_id(matched)
        candidate.metadata["evidence_id"] = eid
        backend = str(candidate.metadata.get("engine") or candidate.metadata.get("ocr_backend") or "ocr")

        if matched is None:
            candidate.metadata.setdefault("canonical_object_id", candidate.object_id)
            candidate.metadata.setdefault(
                "physical_object_id", physical_object_id(identity_from_document_object(candidate))
            )
            candidate.metadata.update({
                "ocr_backend": backend,
                "ocr_status": "added_missing_object",
                "source_object_ids": list(normalize_object_ids(
                    candidate.metadata.get("source_object_ids"), candidate.object_id
                )),
            })
            merged_objects.append(candidate)
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
            "source_object_ids": list(normalize_object_ids(
                matched.metadata.get("source_object_ids"),
                candidate.metadata.get("source_object_ids"),
                matched.object_id,
                candidate.object_id,
            )),
            "ocr_cell_count": candidate_cells,
        })
        existing_physical_id = str(matched.metadata.get("physical_object_id") or "").strip()
        merged.metadata.update(aggregate_identity_metadata(
            merged.object_id,
            [
                identity_from_document_object(matched),
                identity_from_document_object(candidate),
            ],
            existing_physical_id=existing_physical_id,
        ))
        replace_object(matched, merged)

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
        source_object_values = list(normalize_object_ids(analysis.get("source_object_ids")))
        source_physical_values = list(normalize_object_ids(
            analysis.get("source_physical_object_ids")
        ))
        canonical_object_id = (
            source_object_values[0] if len(source_object_values) == 1 else ""
        )
        physical_id = (
            source_physical_values[0] if len(source_physical_values) == 1 else ""
        )
        render_variant = str(analysis.get("render_variant") or "")
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
                "source_object_ids": source_object_values,
                "canonical_object_id": canonical_object_id,
                "physical_object_id": physical_id,
                "confidence": analysis.get("confidence", ""),
                "target_sheet": analysis.get("target_sheet", ""),
                "visual_result_type": analysis.get("visual_result_type", analysis.get("type", "")),
                "visual_structure_type": (
                    analysis.get("chart_type", "")
                    if str(analysis.get("chart_type") or "").strip().casefold()
                    in {"diagram", "infographic", "flow", "strategy_map", "risk_map", "risk_matrix"}
                    else ""
                ),
                "negative_revalidation": dict(analysis.get("negative_revalidation") or {}),
                "render_variant": render_variant,
                "reconstruction_method": analysis.get("reconstruction_method", ""),
                "render_group_id": analysis.get("render_group_id", ""),
                "panel_index": analysis.get("panel_index", 0),
                "panel_count": analysis.get("panel_count", 0),
                "context_only": render_variant in {"full_page_context", "fallback_full_page"},
                "reference_context": (
                    analysis.get("reference_context") is True
                    or str(analysis.get("reference_context") or "").strip().casefold()
                    in {"1", "true", "yes"}
                ),
                "reference_context_hits": list(analysis.get("reference_context_hits") or []),
                "reference_merge_policy": analysis.get("reference_merge_policy", "standard"),
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


@dataclass(frozen=True, slots=True)
class CandidateRenderRegion:
    variant: str
    bbox: tuple[float, float, float, float]
    method: str
    panel_index: int = 0
    panel_count: int = 0


def _page_bounds(page: PageContent) -> fitz.Rect:
    return fitz.Rect(0.0, 0.0, float(page.width or 595.0), float(page.height or 842.0))


def _clipped_rect(
    value: Any,
    bounds: fitz.Rect,
    *,
    min_width: float = 1.0,
    min_height: float = 1.0,
) -> fitz.Rect | None:
    try:
        rect = fitz.Rect(value) & bounds
    except (TypeError, ValueError, AssertionError):
        return None
    if rect.width < min_width or rect.height < min_height:
        return None
    return rect


def _expanded_rect(rect: fitz.Rect, bounds: fitz.Rect, margin: float = 8.0) -> fitz.Rect:
    return fitz.Rect(
        max(bounds.x0, rect.x0 - margin),
        max(bounds.y0, rect.y0 - margin),
        min(bounds.x1, rect.x1 + margin),
        min(bounds.y1, rect.y1 + margin),
    )


def _union_rects(rects: list[fitz.Rect]) -> fitz.Rect | None:
    if not rects:
        return None
    union = fitz.Rect(rects[0])
    for rect in rects[1:]:
        union.include_rect(rect)
    return union


def _axis_overlap(left: fitz.Rect, right: fitz.Rect, *, horizontal: bool) -> float:
    if horizontal:
        overlap = max(0.0, min(left.x1, right.x1) - max(left.x0, right.x0))
        denominator = min(left.width, right.width)
    else:
        overlap = max(0.0, min(left.y1, right.y1) - max(left.y0, right.y0))
        denominator = min(left.height, right.height)
    return overlap / denominator if denominator > 0 else 0.0


def _axis_gap(left: fitz.Rect, right: fitz.Rect, *, horizontal: bool) -> float:
    if horizontal:
        return max(0.0, max(left.x0, right.x0) - min(left.x1, right.x1))
    return max(0.0, max(left.y0, right.y0) - min(left.y1, right.y1))


def _region_components(
    sources: list[tuple[fitz.Rect, str]],
    *,
    gap: float,
) -> list[list[tuple[fitz.Rect, str]]]:
    remaining = list(sources)
    components: list[list[tuple[fitz.Rect, str]]] = []
    while remaining:
        component = [remaining.pop(0)]
        changed = True
        while changed:
            changed = False
            for candidate in list(remaining):
                rect = candidate[0]
                connected = any(
                    (
                        _axis_gap(rect, existing[0], horizontal=True) <= gap
                        and _axis_overlap(rect, existing[0], horizontal=False) >= 0.20
                    )
                    or (
                        _axis_gap(rect, existing[0], horizontal=False) <= gap
                        and _axis_overlap(rect, existing[0], horizontal=True) >= 0.20
                    )
                    for existing in component
                )
                if connected:
                    component.append(candidate)
                    remaining.remove(candidate)
                    changed = True
        components.append(component)
    return components


def _visual_region_sources(
    pdf_page: fitz.Page,
    page: PageContent,
    obj: DocumentObject,
    bounds: fitz.Rect,
) -> list[tuple[fitz.Rect, str]]:
    sources: list[tuple[fitz.Rect, str]] = []
    seen: set[tuple[int, int, int, int, str]] = set()

    def append(value: Any, kind: str) -> None:
        rect = _clipped_rect(value, bounds, min_width=18.0, min_height=12.0)
        if rect is None or (rect.width >= bounds.width * 0.92 and rect.height >= bounds.height * 0.92):
            return
        key = tuple(int(round(item)) for item in (*rect,)) + (kind,)
        if key not in seen:
            seen.add(key)
            sources.append((rect, kind))

    for image in page.images or []:
        if str(image.get("source_kind") or "") == "page_render":
            continue
        placements = image.get("placements") if isinstance(image.get("placements"), list) else []
        if placements:
            for placement in placements:
                append(placement, "image")
        else:
            append(image.get("bbox"), "image")
    try:
        for image_info in pdf_page.get_images(full=True):
            for placement in pdf_page.get_image_rects(image_info[0]):
                append(placement, "image")
    except Exception:
        pass
    if obj.object_type == "table":
        for table in page.table_records or []:
            append(table.get("bbox"), "table")
    return sources


def _vertical_distance(rect: fitz.Rect, caption: fitz.Rect) -> float:
    return max(0.0, caption.y0 - rect.y1, rect.y0 - caption.y1)


def _nearest_visual_component(
    caption: fitz.Rect,
    sources: list[tuple[fitz.Rect, str]],
    bounds: fitz.Rect,
) -> list[tuple[fitz.Rect, str]]:
    components = _region_components(
        sources,
        gap=float(getattr(config, "OCR_MULTI_PANEL_GAP", 24)),
    )
    ranked: list[tuple[float, list[tuple[fitz.Rect, str]]]] = []
    for component in components:
        union = _union_rects([row[0] for row in component])
        if union is None:
            continue
        distance = _vertical_distance(union, caption)
        horizontal_penalty = abs(union.x0 + union.x1 - caption.x0 - caption.x1) * 0.03
        ranked.append((distance + horizontal_penalty, component))
    if not ranked:
        return []
    ranked.sort(key=lambda row: row[0])
    selected = ranked[0][1]
    selected_union = _union_rects([row[0] for row in selected])
    max_distance = max(120.0, bounds.height * 0.38)
    if selected_union is None or _vertical_distance(selected_union, caption) > max_distance:
        return []
    return selected


def _caption_neighbor_intervals(
    obj: DocumentObject,
    page_objects: list[DocumentObject],
    bounds: fitz.Rect,
) -> tuple[fitz.Rect, fitz.Rect]:
    caption = fitz.Rect(obj.bbox)
    other_captions = sorted(
        (
            fitz.Rect(other.bbox)
            for other in page_objects
            if other.object_id != obj.object_id
            and other.bbox
            and other.caption
            and (other.metadata.get("caption_only") or other.metadata.get("missing_native"))
        ),
        key=lambda rect: rect.y0,
    )
    previous_end = max(
        (rect.y1 for rect in other_captions if rect.y1 <= caption.y0),
        default=bounds.y0 + bounds.height * 0.07,
    )
    next_start = min(
        (rect.y0 for rect in other_captions if rect.y0 >= caption.y1),
        default=bounds.y1 - bounds.height * 0.06,
    )
    max_height = bounds.height * float(
        getattr(config, "OCR_CAPTION_REGION_MAX_HEIGHT_RATIO", 0.45)
    )
    upper_y1 = max(bounds.y0 + 1.0, caption.y0 - 4.0)
    upper_y0 = min(
        upper_y1 - 1.0,
        max(previous_end + 4.0, caption.y0 - max_height),
    )
    lower_y0 = min(bounds.y1 - 1.0, caption.y1 + 4.0)
    lower_y1 = max(
        lower_y0 + 1.0,
        min(next_start - 4.0, caption.y1 + max_height),
    )
    upper = fitz.Rect(
        bounds.x0 + 18.0,
        upper_y0,
        bounds.x1 - 18.0,
        upper_y1,
    )
    lower = fitz.Rect(
        bounds.x0 + 18.0,
        lower_y0,
        bounds.x1 - 18.0,
        lower_y1,
    )
    return upper, lower


def _rect_center_inside(rect: fitz.Rect, interval: fitz.Rect) -> bool:
    center_x = (rect.x0 + rect.x1) / 2.0
    center_y = (rect.y0 + rect.y1) / 2.0
    return interval.x0 <= center_x <= interval.x1 and interval.y0 <= center_y <= interval.y1


def _vector_fallback_region(
    pdf_page: fitz.Page,
    page: PageContent,
    obj: DocumentObject,
    page_objects: list[DocumentObject],
    bounds: fitz.Rect,
) -> fitz.Rect | None:
    if not obj.bbox:
        return None
    caption = fitz.Rect(obj.bbox)
    upper, lower = _caption_neighbor_intervals(obj, page_objects, bounds)
    drawing_rects: list[fitz.Rect] = []
    try:
        for drawing in pdf_page.get_drawings():
            rect = _clipped_rect(drawing.get("rect"), bounds, min_width=0.0, min_height=0.0)
            if rect is not None and (rect.width > 0 or rect.height > 0):
                drawing_rects.append(rect)
    except Exception:
        pass
    text_rects = [
        rect
        for block in (page.text_blocks or [])
        if (rect := _clipped_rect(block.get("bbox"), bounds, min_width=4.0, min_height=3.0))
        is not None
        and not rect.intersects(caption)
    ]

    def contents(interval: fitz.Rect) -> tuple[list[fitz.Rect], float]:
        drawings = [rect for rect in drawing_rects if _rect_center_inside(rect, interval)]
        texts = [rect for rect in text_rects if _rect_center_inside(rect, interval)]
        score = min(len(drawings), 120) * 0.20 + min(len(texts), 30) * 1.5
        return [*drawings, *texts], score

    upper_contents, upper_score = contents(upper)
    lower_contents, lower_score = contents(lower)
    if obj.object_type == "table" and lower_contents:
        selected_interval, selected_contents = lower, lower_contents
    elif lower_score > upper_score:
        selected_interval, selected_contents = lower, lower_contents
    else:
        selected_interval, selected_contents = upper, upper_contents
    content_union = _union_rects(selected_contents)
    if content_union is None or content_union.width < 80 or content_union.height < 24:
        content_union = selected_interval
    content_union = content_union & selected_interval
    content_union.include_rect(caption)
    return _expanded_rect(content_union, bounds, margin=8.0)


def reconstruct_candidate_regions(
    pdf_page: fitz.Page,
    page: PageContent,
    obj: DocumentObject,
    page_objects: list[DocumentObject],
) -> list[CandidateRenderRegion]:
    """캡션 프록시를 실제 인접 시각 영역과 결합해 렌더 변형을 만든다."""
    bounds = _page_bounds(page)
    placeholder = bool(obj.metadata.get("caption_only") or obj.metadata.get("missing_native"))
    if obj.bbox and not placeholder:
        rect = _clipped_rect(obj.bbox, bounds, min_width=40.0, min_height=20.0)
        if rect is not None:
            rect = _expanded_rect(rect, bounds)
            return [CandidateRenderRegion("object", tuple(rect), "native_bbox")]

    if not placeholder or not getattr(config, "OCR_SPATIAL_RECONSTRUCTION_ENABLED", True):
        return [CandidateRenderRegion("fallback_full_page", tuple(bounds), "full_page_fallback")]

    caption = _clipped_rect(obj.bbox, bounds, min_width=4.0, min_height=3.0)
    regions: list[CandidateRenderRegion] = []
    if caption is not None:
        upper_interval, lower_interval = _caption_neighbor_intervals(
            obj, page_objects, bounds
        )
        visual_sources = [
            source
            for source in _visual_region_sources(pdf_page, page, obj, bounds)
            if _rect_center_inside(source[0], upper_interval)
            or _rect_center_inside(source[0], lower_interval)
        ]
        component = _nearest_visual_component(
            caption,
            visual_sources,
            bounds,
        )
        component_rects = [row[0] for row in component]
        image_only = bool(component_rects) and all(row[1] == "image" for row in component)
        if len(component_rects) >= 2 and image_only:
            ordered = sorted(component_rects, key=lambda rect: (round(rect.y0, 1), rect.x0))
            for index, rect in enumerate(ordered, start=1):
                regions.append(CandidateRenderRegion(
                    "panel",
                    tuple(_expanded_rect(rect, bounds, margin=5.0)),
                    "adjacent_image_cluster",
                    panel_index=index,
                    panel_count=len(ordered),
                ))
            composite = _union_rects([*ordered, caption])
            if composite is not None:
                regions.append(CandidateRenderRegion(
                    "composite",
                    tuple(_expanded_rect(composite, bounds)),
                    "adjacent_image_cluster",
                    panel_count=len(ordered),
                ))
        elif component_rects:
            object_rect = _union_rects([*component_rects, caption])
            if object_rect is not None:
                regions.append(CandidateRenderRegion(
                    "object",
                    tuple(_expanded_rect(object_rect, bounds)),
                    "adjacent_visual_region",
                ))
        else:
            vector_rect = _vector_fallback_region(pdf_page, page, obj, page_objects, bounds)
            if vector_rect is not None:
                regions.append(CandidateRenderRegion(
                    "object", tuple(vector_rect), "vector_text_envelope"
                ))

    if not regions:
        regions.append(CandidateRenderRegion(
            "fallback_full_page", tuple(bounds), "full_page_fallback"
        ))
    elif getattr(config, "OCR_FULL_PAGE_CONTEXT_ENABLED", True):
        regions.append(CandidateRenderRegion(
            "full_page_context", tuple(bounds), "page_context"
        ))
    return regions


def _render_region_image(pdf_page: fitz.Page, rect: fitz.Rect) -> Image.Image:
    scale = float(getattr(config, "OCR_RENDER_DPI", 200)) / 72.0
    pix = pdf_page.get_pixmap(matrix=fitz.Matrix(scale, scale), clip=rect, alpha=False)
    image = Image.frombytes("RGB", [pix.width, pix.height], pix.samples)
    max_size = int(getattr(config, "MAX_IMAGE_SIZE", 1568))
    if max(image.size) > max_size:
        ratio = max_size / max(image.size)
        image = image.resize(
            (max(1, int(image.width * ratio)), max(1, int(image.height * ratio))),
            Image.LANCZOS,
        )
    return image


def render_ocr_candidate_images(
    document: PDFContent,
    objects: list[DocumentObject],
    decisions: list[TriageDecision],
    *,
    stats: dict[str, Any] | None = None,
) -> list[tuple[PageContent, dict[str, Any]]]:
    """저신뢰 객체를 물리 영역·패널·페이지 문맥 변형으로 지연 렌더링한다."""
    if not document.source_path:
        return []
    page_map = {page.page_number: page for page in document.pages}
    object_map = {obj.object_id: obj for obj in objects}
    objects_by_page: dict[int, list[DocumentObject]] = {}
    for obj in objects:
        objects_by_page.setdefault(obj.page_number, []).append(obj)
    required = [row for row in decisions if row.action == "ocr_required"]
    rendered: list[tuple[PageContent, dict[str, Any]]] = []
    render_cache_enabled = bool(getattr(config, "OCR_RENDER_CACHE_ENABLED", True))
    render_cache: dict[tuple[Any, ...], tuple[str, int, int, int]] = {}
    render_stats: dict[str, int | float] = {
        "render_requests": 0,
        "render_unique": 0,
        "render_reused": 0,
        "render_seconds": 0.0,
        "render_png_bytes": 0,
    }
    try:
        pdf = fitz.open(document.source_path)
    except Exception:
        return []
    try:
        grouped: list[list[TriageDecision]] = []
        for row in required:
            selected: list[TriageDecision] | None = None
            for group in grouped:
                matches = [
                    match_physical_objects(
                        _triage_identity(member), _triage_identity(row)
                    )
                    for member in group
                ]
                if any(result.blocked for result in matches):
                    continue
                if any(result.same for result in matches):
                    selected = group
                    break
            if selected is None:
                grouped.append([row])
            else:
                selected.append(row)
        for rows in grouped:
            row = rows[0]
            physical_key = row.physical_object_id or row.evidence_id or row.object_id
            page = page_map.get(row.page_number)
            candidates = [object_map.get(item.object_id) for item in rows]
            candidates = [obj for obj in candidates if obj is not None]
            obj = max(
                candidates,
                key=lambda item: (
                    bool(item.metadata.get("caption_only") or item.metadata.get("missing_native")),
                    bool(item.caption),
                    bool(item.bbox),
                ),
                default=None,
            )
            if page is None or obj is None or row.page_number < 1 or row.page_number > len(pdf):
                continue
            source_object_ids = list(normalize_object_ids(*(
                [*item.source_object_ids, item.object_id, *item.alias_object_ids]
                for item in rows
            )))
            source_evidence_ids = list(normalize_object_ids(
                *(item.evidence_id for item in rows)
            ))
            source_physical_ids = list(normalize_object_ids(
                *(item.physical_object_id for item in rows)
            ))
            regions = reconstruct_candidate_regions(
                pdf[row.page_number - 1],
                page,
                obj,
                objects_by_page.get(row.page_number, []),
            )
            for region in regions:
                rect = fitz.Rect(region.bbox)
                render_stats["render_requests"] += 1
                render_key = (
                    row.page_number,
                    tuple(float(value) for value in region.bbox),
                    int(getattr(config, "OCR_RENDER_DPI", 200)),
                    int(getattr(config, "MAX_IMAGE_SIZE", 1568)),
                )
                cached_render = render_cache.get(render_key) if render_cache_enabled else None
                if cached_render is not None:
                    image_b64, image_width, image_height, _ = cached_render
                    render_stats["render_reused"] += 1
                else:
                    render_started = time.perf_counter()
                    image = _render_region_image(pdf[row.page_number - 1], rect)
                    buffer = io.BytesIO()
                    image.save(buffer, format="PNG")
                    png_bytes = buffer.getvalue()
                    image_b64 = base64.b64encode(png_bytes).decode("ascii")
                    image_width = image.width
                    image_height = image.height
                    render_stats["render_unique"] += 1
                    render_stats["render_seconds"] += time.perf_counter() - render_started
                    render_stats["render_png_bytes"] += len(png_bytes)
                    if render_cache_enabled:
                        render_cache[render_key] = (
                            image_b64,
                            image_width,
                            image_height,
                            len(png_bytes),
                        )
                base_caption = obj.caption or f"p{row.page_number} {obj.object_type} OCR candidate"
                rendered.append((page, {
                    "base64": image_b64,
                    "width": image_width,
                    "height": image_height,
                    "caption": f"{base_caption} [render:{region.variant}]",
                    "bbox": list(rect),
                    "source_kind": "ocr_candidate",
                    "render_variant": region.variant,
                    "reconstruction_method": region.method,
                    "physical_reconstruction": region.variant != "fallback_full_page",
                    "panel_index": region.panel_index,
                    "panel_count": region.panel_count,
                    "render_group_id": source_physical_ids[0] if source_physical_ids else physical_key,
                    "source_object_ids": source_object_ids,
                    "source_evidence_ids": source_evidence_ids,
                    "source_physical_object_ids": source_physical_ids,
                    "triage_reasons": list(dict.fromkeys(
                        reason
                        for item in rows
                        for reason in item.reasons
                    )),
                    "negative_revalidation_signals": list(dict.fromkeys(
                        reason
                        for item in rows
                        for reason in item.reasons
                        if str(reason).startswith("strong:")
                    )),
                    "ocr_required": True,
                }))
    finally:
        pdf.close()
    render_stats["render_seconds"] = round(float(render_stats["render_seconds"]), 3)
    if stats is not None:
        stats.update(render_stats)
    return rendered
