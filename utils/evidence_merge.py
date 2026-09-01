"""시각 판독값과 원문 객체 사이의 엄격한 근거 ID 매칭 계약."""

from __future__ import annotations

from dataclasses import dataclass
import json
import re
from typing import Any, Iterable

from utils.physical_objects import (
    PhysicalObjectIdentity,
    bbox_iou,
    caption_similarity,
    normalize_object_number,
    normalize_object_ids,
    same_physical_object as identities_match,
)


_OBJECT_NUMBER_RE = re.compile(
    r"\b(?:표|그림|figure|fig\.?)\s*"
    r"[0-9ⅠⅡⅢⅣⅤⅥⅦⅧⅨⅩ]+(?:\s*[-–—.]\s*\d+)?",
    re.IGNORECASE,
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
    evidence_ids: tuple[str, ...] = ()
    physical_object_ids: tuple[str, ...] = ()
    method: str = ""
    corrected: bool = False

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
        return EvidenceMatch("missing", reason="근거 ID 없음", method="carried_evidence_id")
    if len(evidence_ids) > 1:
        return EvidenceMatch(
            "multiple_ids",
            reason=f"복수 근거 ID({len(evidence_ids)}개)",
            evidence_ids=tuple(evidence_ids),
            method="carried_evidence_id",
        )

    evidence_id = evidence_ids[0]
    references = catalog.get(evidence_id, ())
    if not references:
        return EvidenceMatch(
            "unknown",
            evidence_id=evidence_id,
            reason="객체 원장에 없는 근거 ID",
            evidence_ids=(evidence_id,),
            method="carried_evidence_id",
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
            evidence_ids=(evidence_id,),
            physical_object_ids=tuple(dict.fromkeys(
                reference.physical_object_id
                for reference in references
                if reference.physical_object_id
            )),
            method="carried_evidence_id",
        )
    if statuses[0] != "extracted":
        return EvidenceMatch(
            "object_not_extracted",
            evidence_id=evidence_id,
            object_ids=object_ids,
            final_statuses=statuses,
            reason=f"근거 객체 최종상태가 extracted 아님({statuses[0] or '미지정'})",
            evidence_ids=(evidence_id,),
            physical_object_ids=tuple(
                [references[0].physical_object_id]
                if references[0].physical_object_id else []
            ),
            method="carried_evidence_id",
        )
    return EvidenceMatch(
        "exact",
        evidence_id=evidence_id,
        object_ids=object_ids,
        final_statuses=statuses,
        reason="단일 근거 ID와 단일 extracted 객체 정확 일치",
        evidence_ids=(evidence_id,),
        physical_object_ids=tuple(
            [references[0].physical_object_id]
            if references[0].physical_object_id else []
        ),
        method="carried_evidence_id",
    )


def _all_references(
    catalog: dict[str, tuple[EvidenceReference, ...]],
) -> list[EvidenceReference]:
    references: list[EvidenceReference] = []
    seen: set[tuple[str, str, int | None]] = set()
    for evidence_id in sorted(catalog):
        for reference in catalog[evidence_id]:
            key = (reference.evidence_id, reference.object_id, reference.page_number)
            if key not in seen:
                seen.add(key)
                references.append(reference)
    return references


def _reference_group_key(reference: EvidenceReference) -> tuple[Any, ...]:
    if reference.physical_object_id:
        return ("physical", reference.page_number, reference.physical_object_id)
    return (
        "object",
        reference.page_number,
        reference.canonical_object_id or reference.object_id,
    )


def _reference_object_ids(reference: EvidenceReference) -> tuple[str, ...]:
    return normalize_object_ids(
        reference.object_id,
        reference.canonical_object_id,
        reference.alias_object_ids,
    )


def _match_selected_references(
    references: Iterable[EvidenceReference],
    *,
    method: str,
    reason: str,
    input_evidence_ids: Iterable[str] = (),
    ambiguous_status: str = "multiple_objects",
) -> EvidenceMatch:
    selected = list(references)
    input_ids = tuple(normalize_evidence_ids(list(input_evidence_ids)))
    if not selected:
        return EvidenceMatch(
            "missing",
            reason=f"{reason}: 일치 객체 없음",
            evidence_ids=input_ids,
            method=method,
        )

    groups: dict[tuple[Any, ...], list[EvidenceReference]] = {}
    for reference in selected:
        groups.setdefault(_reference_group_key(reference), []).append(reference)
    matched_evidence_ids = tuple(dict.fromkeys(
        reference.evidence_id for reference in selected if reference.evidence_id
    ))
    object_ids = tuple(dict.fromkeys(
        object_id
        for reference in selected
        for object_id in _reference_object_ids(reference)
    ))
    physical_ids = tuple(dict.fromkeys(
        reference.physical_object_id
        for reference in selected
        if reference.physical_object_id
    ))
    statuses = tuple(dict.fromkeys(
        reference.final_status for reference in selected
    ))
    if len(groups) != 1:
        return EvidenceMatch(
            ambiguous_status,
            object_ids=object_ids,
            final_statuses=statuses,
            reason=f"{reason}: 서로 다른 물리 객체 {len(groups)}개",
            evidence_ids=matched_evidence_ids,
            physical_object_ids=physical_ids,
            method=method,
        )

    current = [
        reference for reference in selected
        if reference.evidence_id in input_ids
    ]
    preferred_pool = current or selected
    preferred = max(
        preferred_pool,
        key=lambda reference: (
            reference.final_status == "extracted",
            bool(reference.number),
            not reference.context_only,
            not reference.render_proxy,
            bool(reference.bbox),
        ),
    )
    if preferred.final_status != "extracted":
        return EvidenceMatch(
            "object_not_extracted",
            evidence_id=preferred.evidence_id,
            object_ids=object_ids,
            final_statuses=statuses,
            reason=f"{reason}: 근거 객체 최종상태가 extracted 아님({preferred.final_status})",
            evidence_ids=matched_evidence_ids,
            physical_object_ids=physical_ids,
            method=method,
            corrected=(len(input_ids) != 1 or preferred.evidence_id not in input_ids),
        )
    return EvidenceMatch(
        "exact",
        evidence_id=preferred.evidence_id,
        object_ids=object_ids,
        final_statuses=statuses,
        reason=reason,
        evidence_ids=matched_evidence_ids,
        physical_object_ids=physical_ids,
        method=method,
        corrected=(len(input_ids) != 1 or preferred.evidence_id not in input_ids),
    )


def _observation_value(observation: dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = observation.get(key)
        if value not in (None, "", [], ()):
            return value
    return None


def _observation_page(observation: dict[str, Any]) -> int | None:
    return _normalize_page(_observation_value(observation, "page_number", "페이지"))


def _same_page_references(
    references: Iterable[EvidenceReference],
    page: int | None,
) -> list[EvidenceReference]:
    if page is None:
        return list(references)
    return [reference for reference in references if reference.page_number == page]


def resolve_observation_evidence(
    observation: dict[str, Any],
    catalog: dict[str, tuple[EvidenceReference, ...]],
    *,
    enabled: bool = True,
) -> EvidenceMatch:
    """관찰값을 하나의 원문 물리 객체에 보수적으로 귀속한다.

    명시 표·그림 번호는 잘못 운반된 근거 ID보다 우선한다. 한 관찰 제목에
    서로 다른 번호가 함께 있거나 강한 선택자가 복수 객체를 가리키면 자동
    병합을 중단한다. 강한 신호가 없을 때만 기존 근거 ID 계약으로 폴백한다.
    """
    evidence_ids = normalize_evidence_ids(
        observation.get("source_evidence_ids"),
        observation.get("근거ID목록"),
        observation.get("근거ID"),
    )
    if not enabled:
        return match_evidence(evidence_ids, catalog)

    references = _all_references(catalog)
    page = _observation_page(observation)
    same_page = _same_page_references(references, page)
    title = str(_observation_value(
        observation, "title", "제목", "caption", "캡션"
    ) or "").strip()

    title_numbers = tuple(dict.fromkeys(
        normalized
        for match in _OBJECT_NUMBER_RE.finditer(title)
        if (normalized := normalize_object_number(match.group(0)))
    ))
    if len(title_numbers) > 1:
        matched = [
            reference
            for reference in same_page
            if normalize_object_number(reference.number or reference.caption) in title_numbers
        ]
        return _match_selected_references(
            matched,
            method="mixed_object_numbers",
            reason=f"관찰 제목에 복수 표·그림 번호({len(title_numbers)}개)",
            input_evidence_ids=evidence_ids,
            ambiguous_status="mixed_objects",
        ) if matched else EvidenceMatch(
            "mixed_objects",
            reason=f"관찰 제목에 복수 표·그림 번호({len(title_numbers)}개)",
            evidence_ids=tuple(evidence_ids),
            method="mixed_object_numbers",
        )
    if len(title_numbers) == 1:
        number = title_numbers[0]
        explicit = [
            reference for reference in same_page
            if reference.number and normalize_object_number(reference.number) == number
        ]
        caption_derived = [
            reference for reference in same_page
            if not reference.number and normalize_object_number(reference.caption) == number
        ]
        selected = explicit or caption_derived
        if selected:
            return _match_selected_references(
                selected,
                method=("object_number_explicit" if explicit else "object_number_caption"),
                reason=f"명시 객체 번호 정확 일치({number})",
                input_evidence_ids=evidence_ids,
            )

    deferred_ambiguity: EvidenceMatch | None = None
    source_object_ids = set(normalize_object_ids(
        observation.get("source_object_ids"),
        observation.get("원본객체ID목록"),
        observation.get("원본객체ID"),
    ))
    if source_object_ids:
        selected = [
            reference for reference in same_page
            if source_object_ids.intersection(_reference_object_ids(reference))
        ]
        if selected:
            selected_match = _match_selected_references(
                selected,
                method="source_object_id",
                reason="원본 객체 ID 정확 일치",
                input_evidence_ids=evidence_ids,
            )
            if selected_match.exact:
                return selected_match
            deferred_ambiguity = selected_match

    render_group_id = str(_observation_value(
        observation, "render_group_id", "렌더그룹ID"
    ) or "").strip()
    panel_index = _normalize_positive_int(_observation_value(
        observation, "panel_index", "패널인덱스"
    ))
    if render_group_id and panel_index is not None:
        selected = [
            reference for reference in same_page
            if reference.render_group_id == render_group_id
            and reference.panel_index == panel_index
        ]
        if selected:
            selected_match = _match_selected_references(
                selected,
                method="render_group_panel",
                reason="렌더 그룹과 패널 인덱스 정확 일치",
                input_evidence_ids=evidence_ids,
            )
            if selected_match.exact:
                return selected_match
            deferred_ambiguity = selected_match

    source_bbox = _normalize_bbox(_observation_value(
        observation, "source_bbox", "근거좌표"
    ))
    if source_bbox is not None:
        selected = [
            reference for reference in same_page
            if bbox_iou(source_bbox, reference.bbox) >= 0.82
        ]
        if selected:
            selected_match = _match_selected_references(
                selected,
                method="bbox_iou",
                reason="동일 페이지 좌표 영역 정확 중첩(IoU>=0.82)",
                input_evidence_ids=evidence_ids,
            )
            if selected_match.exact:
                return selected_match
            deferred_ambiguity = selected_match

    if title:
        expected = PhysicalObjectIdentity(
            object_id="observation",
            page_number=page,
            object_type="visual",
            caption=title,
        )
        scored = [
            (reference, caption_similarity(expected, PhysicalObjectIdentity(
                object_id=reference.object_id,
                page_number=reference.page_number,
                object_type=reference.object_type,
                caption=reference.caption,
                number=reference.number,
            )))
            for reference in same_page
            if reference.caption
        ]
        strong = [(reference, score) for reference, score in scored if score >= 0.90]
        if strong:
            best_score = max(score for _reference, score in strong)
            selected = [
                reference for reference, score in strong
                if best_score - score < 0.12
            ]
            selected_match = _match_selected_references(
                selected,
                method="caption_unique",
                reason=f"동일 페이지 캡션 고유 일치(score={best_score:.2f})",
                input_evidence_ids=evidence_ids,
            )
            if selected_match.exact:
                return selected_match
            deferred_ambiguity = selected_match

    source_physical_ids = set(normalize_object_ids(
        observation.get("source_physical_object_ids"),
        observation.get("물리객체ID목록"),
        observation.get("물리객체ID"),
    ))
    if source_physical_ids:
        selected = [
            reference for reference in same_page
            if reference.physical_object_id in source_physical_ids
        ]
        if selected:
            selected_match = _match_selected_references(
                selected,
                method="physical_object_id",
                reason="물리 객체 ID 정확 일치",
                input_evidence_ids=evidence_ids,
            )
            if selected_match.exact:
                return selected_match
            deferred_ambiguity = selected_match

    return deferred_ambiguity or match_evidence(evidence_ids, catalog)
