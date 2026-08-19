"""원문 객체와 결과 객체를 보수적으로 연결하는 공통 P5 계약."""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Iterable

from utils.evidence_merge import normalize_evidence_ids
from utils.physical_objects import (
    PhysicalObjectIdentity,
    caption_similarity,
    normalize_object_number,
)


_NUMBER_ANCHOR_RE = re.compile(
    r"(?<![A-Za-z가-힣])[-+−]?\d[\d,]*(?:\.\d+)?"
    r"(?:\s*(?:%|t\s*co2(?:eq|e)?|co2(?:eq|e)?|천\s*톤|백만\s*톤|억원|백만원|만원|"
    r"명|대|건|개|km|ha|℃|mm|지수|등급))?",
    re.IGNORECASE,
)
_WORD_ANCHOR_RE = re.compile(r"[A-Za-z가-힣][A-Za-z가-힣0-9·ㆍ()/+.-]{1,}")
_GENERIC_ANCHORS = {
    "그림", "figure", "fig", "표", "table", "자료", "단위", "비고", "합계",
    "구분", "기타", "관련시트", "현황", "계획", "목표", "그래프", "차트",
}


@dataclass(frozen=True, slots=True)
class ObjectEvidenceDescriptor:
    """매칭에 필요한 최소 객체 식별 정보."""

    key: str
    page: int | None
    evidence_ids: tuple[str, ...] = ()
    object_ids: tuple[str, ...] = ()
    number: str = ""
    caption: str = ""
    content: str = ""


@dataclass(frozen=True, slots=True)
class ObjectEvidenceMatch:
    """3단계 근거 확인 결과."""

    status: str
    matched_keys: tuple[str, ...] = ()
    matched_evidence_ids: tuple[str, ...] = ()
    reason: str = ""
    caption_score: float = 0.0
    numeric_hits: tuple[str, ...] = ()
    lexical_hits: tuple[str, ...] = ()

    @property
    def confirmed(self) -> bool:
        return self.status in {"exact_id", "exact_object_id", "composite"}


def descriptor(
    *,
    key: Any,
    page: Any = None,
    evidence_ids: Any = None,
    object_ids: Any = None,
    number: Any = "",
    caption: Any = "",
    content: Any = "",
) -> ObjectEvidenceDescriptor:
    """입력 표현을 안정적인 descriptor로 정규화한다."""
    try:
        normalized_page = int(page) if page is not None and not isinstance(page, bool) else None
    except (TypeError, ValueError):
        normalized_page = None
    if normalized_page is not None and normalized_page < 1:
        normalized_page = None
    return ObjectEvidenceDescriptor(
        key=str(key or "").strip(),
        page=normalized_page,
        evidence_ids=tuple(normalize_evidence_ids(evidence_ids)),
        object_ids=_normalize_ids(object_ids),
        number=str(number or "").strip(),
        caption=str(caption or "").strip(),
        content=str(content or "").strip(),
    )


def _normalize_ids(values: Any) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()

    def append(value: Any) -> None:
        if isinstance(value, (list, tuple, set)):
            for item in value:
                append(item)
            return
        text = str(value or "").strip()
        if text and text not in seen:
            seen.add(text)
            output.append(text)

    append(values)
    return tuple(output)


def _identity(value: ObjectEvidenceDescriptor) -> PhysicalObjectIdentity:
    return PhysicalObjectIdentity(
        object_id=value.object_ids[0] if value.object_ids else value.key,
        page_number=value.page,
        object_type="visual",
        evidence_id=value.evidence_ids[0] if len(value.evidence_ids) == 1 else "",
        caption=value.caption,
        number=value.number,
        alias_object_ids=value.object_ids[1:],
    )


def _content_anchors(value: Any) -> tuple[set[str], set[str]]:
    text = str(value or "")
    numeric = {
        re.sub(r"\s+", "", match.group(0)).casefold().replace("−", "-")
        for match in _NUMBER_ANCHOR_RE.finditer(text)
        if match.group(0).strip()
    }
    lexical = {
        token.casefold()
        for token in _WORD_ANCHOR_RE.findall(text)
        if len(token) >= 2 and token.casefold() not in _GENERIC_ANCHORS
    }
    return numeric, lexical


def _group_keys(values: Iterable[ObjectEvidenceDescriptor]) -> set[str]:
    groups: set[str] = set()
    for value in values:
        if value.evidence_ids:
            groups.add("evidence:" + "|".join(value.evidence_ids))
        elif value.object_ids:
            groups.add("object:" + "|".join(value.object_ids))
        else:
            groups.add("row:" + value.key)
    return groups


def match_object_evidence(
    expected: ObjectEvidenceDescriptor,
    candidates: Iterable[ObjectEvidenceDescriptor],
    *,
    ambiguous_evidence_ids: Iterable[str] = (),
) -> ObjectEvidenceMatch:
    """정확 ID, 복합 근거, 불충분 순으로 객체를 확인한다."""
    values = list(candidates)
    ambiguous = set(normalize_evidence_ids(list(ambiguous_evidence_ids)))
    expected_ids = set(expected.evidence_ids)

    exact = [value for value in values if expected_ids.intersection(value.evidence_ids)]
    if exact:
        matched_ids = tuple(dict.fromkeys(
            evidence_id
            for value in exact
            for evidence_id in value.evidence_ids
            if evidence_id in expected_ids
        ))
        if expected_ids.intersection(ambiguous):
            return ObjectEvidenceMatch(
                "ambiguous", tuple(value.key for value in exact), matched_ids,
                "동일 근거 ID가 복수 원문 객체에 연결됨",
            )
        if any(len(value.evidence_ids) != 1 for value in exact):
            return ObjectEvidenceMatch(
                "ambiguous", tuple(value.key for value in exact), matched_ids,
                "결과 행 하나에 복수 근거 ID가 기록됨",
            )
        if any(
            expected.page is not None
            and value.page is not None
            and expected.page != value.page
            for value in exact
        ):
            return ObjectEvidenceMatch(
                "ambiguous", tuple(value.key for value in exact), matched_ids,
                "근거 ID는 같지만 출처페이지가 충돌함",
            )
        return ObjectEvidenceMatch(
            "exact_id", tuple(value.key for value in exact), matched_ids,
            "단일 근거 ID 정확 일치",
        )

    expected_object_ids = set(expected.object_ids)
    exact_objects = [
        value for value in values
        if expected_object_ids.intersection(value.object_ids)
    ]
    if exact_objects:
        if any(
            expected.page is not None
            and value.page is not None
            and expected.page != value.page
            for value in exact_objects
        ):
            return ObjectEvidenceMatch(
                "ambiguous", tuple(value.key for value in exact_objects), (),
                "객체 ID는 같지만 출처페이지가 충돌함",
            )
        return ObjectEvidenceMatch(
            "exact_object_id", tuple(value.key for value in exact_objects),
            tuple(dict.fromkeys(
                evidence_id for value in exact_objects for evidence_id in value.evidence_ids
            )),
            "단일 물리 객체 ID 정확 일치",
        )

    same_page = [
        value for value in values
        if expected.page is None or value.page is None or expected.page == value.page
    ]
    expected_number = normalize_object_number(expected.number or expected.caption)
    expected_numeric, expected_lexical = _content_anchors(expected.content)
    composite: list[tuple[ObjectEvidenceDescriptor, float, set[str], set[str]]] = []
    partial: list[tuple[ObjectEvidenceDescriptor, float, set[str], set[str]]] = []

    for value in same_page:
        candidate_number = normalize_object_number(value.number or value.caption)
        number_match = bool(expected_number and expected_number == candidate_number)
        score = caption_similarity(_identity(expected), _identity(value))
        candidate_numeric, candidate_lexical = _content_anchors(value.content)
        numeric_hits = expected_numeric.intersection(candidate_numeric)
        lexical_hits = expected_lexical.intersection(candidate_lexical)
        content_ok = (
            bool(numeric_hits)
            if expected_numeric
            else len(lexical_hits) >= 2 or (not expected_lexical and score >= 0.90)
        )
        if number_match and score >= 0.72 and content_ok:
            composite.append((value, score, numeric_hits, lexical_hits))
        elif number_match and score >= 0.72:
            partial.append((value, score, numeric_hits, lexical_hits))

    if composite:
        descriptors = [value for value, _score, _numeric, _lexical in composite]
        groups = _group_keys(descriptors)
        matched_ids = tuple(dict.fromkeys(
            evidence_id for value in descriptors for evidence_id in value.evidence_ids
        ))
        if len(groups) > 1:
            return ObjectEvidenceMatch(
                "ambiguous", tuple(value.key for value in descriptors), matched_ids,
                f"복합 근거와 일치하는 결과 객체가 복수임({len(groups)}개)",
                max(score for _value, score, _numeric, _lexical in composite),
            )
        numeric_hits = tuple(sorted({hit for _value, _score, hits, _lexical in composite for hit in hits}))
        lexical_hits = tuple(sorted({hit for _value, _score, _numeric, hits in composite for hit in hits}))
        return ObjectEvidenceMatch(
            "composite", tuple(value.key for value in descriptors), matched_ids,
            "표·그림 번호, 캡션, 핵심 내용 복합 일치",
            max(score for _value, score, _numeric, _lexical in composite),
            numeric_hits,
            lexical_hits,
        )

    if partial:
        descriptors = [value for value, _score, _numeric, _lexical in partial]
        return ObjectEvidenceMatch(
            "partial", tuple(value.key for value in descriptors),
            tuple(dict.fromkeys(
                evidence_id for value in descriptors for evidence_id in value.evidence_ids
            )),
            "번호와 캡션은 일치하지만 핵심 내용 근거가 부족함",
            max(score for _value, score, _numeric, _lexical in partial),
        )

    return ObjectEvidenceMatch("missing", reason="객체 정체성을 확인할 근거가 없음")
