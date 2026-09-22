"""시각 판독값과 원문 객체 사이의 엄격한 근거 ID 매칭 계약."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable

from utils.physical_objects import (
    PhysicalObjectIdentity,
    normalize_object_ids,
    same_physical_object as identities_match,
)


@dataclass(frozen=True, slots=True)
class EvidenceReference:
    evidence_id: str
    object_id: str
    final_status: str
    page_number: int | None = None
    object_type: str = ""
    bbox: tuple[float, float, float, float] | None = None
    caption: str = ""
    number: str = ""
    caption_only: bool = False
    render_proxy: bool = False
    missing_native: bool = False
    engine: str = ""
    canonical_object_id: str = ""
    alias_object_ids: tuple[str, ...] = ()
    physical_object_id: str = ""
    render_group_id: str = ""
    render_variant: str = ""
    panel_index: int | None = None
    panel_count: int = 0
    context_only: bool = False


@dataclass(frozen=True, slots=True)
class EvidenceMatch:
    """자동 병합 전에 확인한 근거 ID 매칭 결과."""

    status: str
    evidence_id: str = ""
    object_ids: tuple[str, ...] = ()
    final_statuses: tuple[str, ...] = ()
    reason: str = ""

    @property
    def exact(self) -> bool:
        return self.status == "exact"


def canonical_evidence_id(value: Any) -> str:
    """Normalize transport formatting while preserving the stable evidence key."""
    text = str(value or "").strip().strip('"\'')
    text = re.sub(r"\s+", "", text)
    return text.casefold() if text.startswith("EV-") or text.startswith("Ev-") else text


def normalize_evidence_ids(*values: Any) -> list[str]:
    """문자열/배열 형태의 근거 ID를 입력 순서대로 중복 없이 정규화한다."""
    normalized: list[str] = []
    seen: set[str] = set()

    def append(value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                append(item)
            return
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                try:
                    parsed = json.loads(stripped)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    append(parsed)
                    return
            if "|" in stripped:
                append([item for item in stripped.split("|") if item.strip()])
                return
        text = canonical_evidence_id(value)
        if not text or text in seen:
            return
        seen.add(text)
        normalized.append(text)

    for value in values:
        append(value)
    return normalized


def _reference_from_document_object(row: dict[str, Any]) -> EvidenceReference | None:
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    evidence_id = canonical_evidence_id(
        metadata.get("evidence_id")
        or metadata.get("source_evidence_id")
        or row.get("evidence_id")
        or row.get("source_evidence_id")
        or ""
    )
    if not evidence_id:
        return None
    raw_object_id = str(row.get("object_id") or metadata.get("source_object_id") or "").strip()
    canonical_object_id = str(metadata.get("canonical_object_id") or raw_object_id).strip()
    aliases = tuple(
        value
        for value in normalize_object_ids(
            raw_object_id if raw_object_id != canonical_object_id else "",
            metadata.get("alias_object_ids"),
            metadata.get("duplicate_object_ids"),
        )
        if value != canonical_object_id
    )
    final_status = str(
        metadata.get("final_status") or row.get("final_status") or "needs_review"
    ).strip()
    bbox = _normalize_bbox(row.get("bbox"))
    page_number = _normalize_page(row.get("page_number"))
    return EvidenceReference(
        evidence_id,
        canonical_object_id or f"unknown:{evidence_id}",
        final_status,
        page_number=page_number,
        object_type=str(row.get("object_type") or "").strip(),
        bbox=bbox,
        caption=str(row.get("caption") or metadata.get("caption") or "").strip(),
        number=str(row.get("number") or "").strip(),
        caption_only=bool(metadata.get("caption_only")),
        render_proxy=bool(metadata.get("render_proxy")),
        missing_native=bool(metadata.get("missing_native")),
        engine=str(metadata.get("engine") or metadata.get("ocr_backend") or "").strip(),
        canonical_object_id=canonical_object_id,
        alias_object_ids=aliases,
        physical_object_id=str(metadata.get("physical_object_id") or "").strip(),
        render_group_id=str(metadata.get("render_group_id") or "").strip(),
        render_variant=str(metadata.get("render_variant") or "").strip(),
        panel_index=_normalize_positive_int(metadata.get("panel_index")),
        panel_count=_normalize_positive_int(metadata.get("panel_count")) or 0,
        context_only=bool(metadata.get("context_only")),
    )


def _reference_from_triage(row: dict[str, Any]) -> EvidenceReference | None:
    evidence_id = canonical_evidence_id(row.get("evidence_id"))
    if not evidence_id:
        return None
    object_id = str(row.get("object_id") or "").strip() or f"unknown:{evidence_id}"
    canonical_object_id = str(row.get("canonical_object_id") or object_id).strip()
    final_status = str(row.get("final_status") or "needs_review").strip()
    reasons = row.get("reasons") if isinstance(row.get("reasons"), list) else []
    render_proxy = bool(
        row.get("render_proxy")
        or row.get("action") == "transport_only"
        or "page_render_transport" in reasons
    )
    missing_native = bool(
        row.get("missing_native") or "native_table_missing" in reasons
    )
    aliases = tuple(
        value
        for value in normalize_object_ids(
            row.get("alias_object_ids"),
            row.get("duplicate_object_ids"),
        )
        if value != canonical_object_id
    )
    return EvidenceReference(
        evidence_id,
        canonical_object_id,
        final_status,
        page_number=_normalize_page(row.get("page_number")),
        object_type=str(row.get("object_type") or "").strip(),
        bbox=_normalize_bbox(row.get("bbox")),
        caption=str(row.get("caption") or "").strip(),
        number=str(row.get("number") or "").strip(),
        caption_only=bool(row.get("caption_only")),
        render_proxy=render_proxy,
        missing_native=missing_native,
        engine=str(row.get("engine") or row.get("backend") or "").strip(),
        canonical_object_id=canonical_object_id,
        alias_object_ids=aliases,
        physical_object_id=str(row.get("physical_object_id") or "").strip(),
        render_group_id=str(row.get("render_group_id") or "").strip(),
        render_variant=str(row.get("render_variant") or "").strip(),
        panel_index=_normalize_positive_int(row.get("panel_index")),
        panel_count=_normalize_positive_int(row.get("panel_count")) or 0,
        context_only=bool(row.get("context_only")),
    )


def _normalize_page(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        page = int(value)
    except (TypeError, ValueError):
        return None
    return page if page > 0 else None


def _normalize_positive_int(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _normalize_bbox(value: Any) -> tuple[float, float, float, float] | None:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        bbox = tuple(float(item) for item in value)
    except (TypeError, ValueError):
        return None
    return bbox  # type: ignore[return-value]


def _same_physical_object(left: EvidenceReference, right: EvidenceReference) -> bool:
    def identity(reference: EvidenceReference) -> PhysicalObjectIdentity:
        return PhysicalObjectIdentity(
            object_id=reference.object_id,
            page_number=reference.page_number,
            object_type=reference.object_type,
            evidence_id=reference.evidence_id,
            bbox=reference.bbox,
            caption=reference.caption,
            number=reference.number,
            engine=reference.engine,
            caption_only=reference.caption_only,
            render_proxy=reference.render_proxy,
            missing_native=reference.missing_native,
            canonical_object_id=reference.canonical_object_id,
            physical_object_id=reference.physical_object_id,
            alias_object_ids=reference.alias_object_ids,
            render_group_id=reference.render_group_id,
            render_variant=reference.render_variant,
            panel_index=reference.panel_index,
            panel_count=reference.panel_count,
            context_only=reference.context_only,
        )

    return identities_match(identity(left), identity(right))


def _merge_reference(left: EvidenceReference, right: EvidenceReference) -> EvidenceReference:
    priority = {"extracted": 4, "no_data": 3, "not_relevant": 2, "needs_review": 1}
    preferred = max(
        (left, right),
        key=lambda ref: (
            priority.get(ref.final_status, 0),
            not ref.caption_only,
            not ref.render_proxy,
            bool(ref.bbox),
        ),
    )
    aliases = tuple(dict.fromkeys(
        [
            *left.alias_object_ids,
            left.object_id,
            *right.alias_object_ids,
            right.object_id,
        ]
    ))
    return EvidenceReference(
        evidence_id=preferred.evidence_id,
        object_id=preferred.object_id,
        final_status=preferred.final_status,
        page_number=preferred.page_number,
        object_type=preferred.object_type,
        bbox=preferred.bbox,
        caption=preferred.caption or left.caption or right.caption,
        number=preferred.number or left.number or right.number,
        caption_only=preferred.caption_only,
        render_proxy=preferred.render_proxy,
        missing_native=preferred.missing_native,
        engine=preferred.engine,
        canonical_object_id=preferred.canonical_object_id or preferred.object_id,
        alias_object_ids=aliases,
        physical_object_id=(
            preferred.physical_object_id
            or left.physical_object_id
            or right.physical_object_id
        ),
        render_group_id=preferred.render_group_id or left.render_group_id or right.render_group_id,
        render_variant=preferred.render_variant or left.render_variant or right.render_variant,
        panel_index=(
            preferred.panel_index
            if preferred.panel_index is not None
            else left.panel_index if left.panel_index is not None else right.panel_index
        ),
        panel_count=max(preferred.panel_count, left.panel_count, right.panel_count),
        context_only=preferred.context_only and left.context_only and right.context_only,
    )


def build_evidence_catalog(
    document_objects: Iterable[dict[str, Any]] | None,
    triage_rows: Iterable[dict[str, Any]] | None = None,
) -> dict[str, tuple[EvidenceReference, ...]]:
    """객체·triage 원장을 근거 ID별 고유 객체 목록으로 만든다."""
    catalog: dict[str, list[EvidenceReference]] = {}

    def add(reference: EvidenceReference | None) -> None:
        if reference is None:
            return
        references = catalog.setdefault(reference.evidence_id, [])
        for index, current in enumerate(references):
            if _same_physical_object(current, reference):
                references[index] = _merge_reference(current, reference)
                return
        references.append(reference)

    for row in document_objects or []:
        if isinstance(row, dict):
            add(_reference_from_document_object(row))
    for row in triage_rows or []:
        if isinstance(row, dict):
            add(_reference_from_triage(row))

    return {
        evidence_id: tuple(sorted(references, key=lambda ref: ref.object_id))
        for evidence_id, references in catalog.items()
    }


def match_evidence(
    evidence_values: Any,
    catalog: dict[str, tuple[EvidenceReference, ...]],
) -> EvidenceMatch:
    """정확히 한 근거 ID가 정확히 한 extracted 객체에 연결되는지 판정한다."""
    evidence_ids = normalize_evidence_ids(evidence_values)
    if not evidence_ids:
        return EvidenceMatch("missing", reason="근거 ID 없음")
    if len(evidence_ids) > 1:
        return EvidenceMatch(
            "multiple_ids",
            reason=f"복수 근거 ID({len(evidence_ids)}개)",
        )

    evidence_id = evidence_ids[0]
    references = catalog.get(evidence_id, ())
    if not references:
        return EvidenceMatch(
            "unknown",
            evidence_id=evidence_id,
            reason="객체 원장에 없는 근거 ID",
        )
    object_ids = tuple(dict.fromkeys(
        object_id
        for reference in references
        for object_id in (reference.object_id, *reference.alias_object_ids)
    ))
    statuses = tuple(reference.final_status for reference in references)
    if len(references) != 1:
        return EvidenceMatch(
            "multiple_objects",
            evidence_id=evidence_id,
            object_ids=object_ids,
            final_statuses=statuses,
            reason=f"동일 근거 ID에 복수 객체 매칭({len(references)}개)",
        )
    if statuses[0] != "extracted":
        return EvidenceMatch(
            "object_not_extracted",
            evidence_id=evidence_id,
            object_ids=object_ids,
            final_statuses=statuses,
            reason=f"근거 객체 최종상태가 extracted 아님({statuses[0] or '미지정'})",
        )
    return EvidenceMatch(
        "exact",
        evidence_id=evidence_id,
        object_ids=object_ids,
        final_statuses=statuses,
        reason="단일 근거 ID와 단일 extracted 객체 정확 일치",
    )
