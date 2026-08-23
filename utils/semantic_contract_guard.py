"""Conservative semantic guards for sheets prone to row multiplication.

The guards only quarantine rows when another canonical sheet carries the same
meaning or when the row does not satisfy the minimum sheet contract. They do
not use municipality names, fixed pages, or document-specific cell locations.
"""

from __future__ import annotations

import json
import math
import re
import unicodedata
from collections import defaultdict
from typing import Any, Iterable


_TARGET_VALUE_FIELDS = (
    "기준배출량",
    "배출전망",
    "목표감축량",
    "목표배출량",
    "감축률",
)
_DETAILED_TARGET_LEVELS = {"세부사업", "연차경로"}
_TARGET_METRIC_LABELS = {
    "기준배출량",
    "배출전망",
    "bau",
    "목표감축량",
    "목표배출량",
    "감축률",
    "총배출량",
    "순배출량",
    "감축량",
    "배출량",
}
_EVIDENCE_FIELDS = (
    "근거ID",
    "근거ID목록",
    "source_evidence_id",
    "source_evidence_ids",
    "evidence_id",
)
_PROJECT_NAME_FIELDS = (
    "_사업명힌트",
    "사업명힌트",
    "사업명",
    "과제명",
    "감축사업명",
)

_FOUNDATION_AREAS = {
    "적응대책",
    "공유재산",
    "국제협력",
    "교육소통",
    "녹색성장",
    "청정에너지",
    "정의로운전환",
    "인력양성",
}
_FOUNDATION_POLICY_DETAIL_FIELDS = (
    "정책방향",
    "대상",
    "주관부서",
    "기간",
    "연계적응과제",
)
_FOUNDATION_RISK_FIELDS = (
    "평가유형",
    "기후변수",
    "시나리오",
    "기준기간",
    "미래기간",
    "공간단위",
    "부문",
    "리스크항목",
    "취약성지표",
    "리스크등급",
    "방법론",
    "자료출처",
)


def _has_value(value: Any) -> bool:
    return value is not None and str(value).strip() != ""


def _normalize_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).casefold()
    return re.sub(r"[^0-9a-z가-힣]+", "", text)


def _iter_values(value: Any) -> Iterable[Any]:
    if isinstance(value, dict):
        for item in value.values():
            yield from _iter_values(item)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_values(item)
    elif value is not None:
        yield value


def _pages(row: dict[str, Any]) -> tuple[int, ...]:
    output: set[int] = set()
    for field in ("출처페이지", "출처페이지추정", "page_number", "페이지"):
        for value in _iter_values(row.get(field)):
            if isinstance(value, bool):
                continue
            if isinstance(value, (int, float)) and int(value) > 0:
                output.add(int(value))
                continue
            for token in re.findall(r"(?:p|P)?\s*(\d{1,4})", str(value or "")):
                page = int(token)
                if page > 0:
                    output.add(page)
    return tuple(sorted(output))


def _ids(row: dict[str, Any]) -> tuple[str, ...]:
    output: list[str] = []
    seen: set[str] = set()
    metadata = row.get("metadata") if isinstance(row.get("metadata"), dict) else {}
    for source in (row, metadata):
        for field in _EVIDENCE_FIELDS:
            for value in _iter_values(source.get(field)):
                text = str(value or "").strip()
                if text.startswith("["):
                    try:
                        parsed = json.loads(text)
                    except json.JSONDecodeError:
                        parsed = None
                    if isinstance(parsed, list):
                        for item in parsed:
                            normalized = str(item or "").strip()
                            if normalized and normalized not in seen:
                                seen.add(normalized)
                                output.append(normalized)
                        continue
                for part in text.split("|"):
                    normalized = part.strip()
                    if normalized and normalized not in seen:
                        seen.add(normalized)
                        output.append(normalized)
    return tuple(output)


def _number(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        number = float(str(value).replace(",", "").replace("%", "").strip())
    except (TypeError, ValueError):
        return None
    return number if math.isfinite(number) else None


def _same_number(left: Any, right: Any) -> bool:
    left_number = _number(left)
    right_number = _number(right)
    if left_number is None or right_number is None:
        return False
    scale = max(abs(left_number), abs(right_number), 1.0)
    return abs(left_number - right_number) / scale <= 0.005


def _project_names(row: dict[str, Any]) -> set[str]:
    return {
        normalized
        for field in _PROJECT_NAME_FIELDS
        if (normalized := _normalize_text(row.get(field)))
    }


def _names_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    left_names = _project_names(left)
    right_names = _project_names(right)
    return any(
        left_name == right_name
        or (
            min(len(left_name), len(right_name)) >= 5
            and (left_name in right_name or right_name in left_name)
        )
        for left_name in left_names
        for right_name in right_names
    )


def _exact_evidence_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(set(_ids(left)) & set(_ids(right)))


def _page_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    return bool(set(_pages(left)) & set(_pages(right)))


def _record(sheet_key: str, row: dict[str, Any], *reasons: str) -> dict[str, Any]:
    return {
        "sheet_key": sheet_key,
        "status": "quarantined",
        "reason_codes": list(dict.fromkeys(reason for reason in reasons if reason)),
        "source_pages": list(_pages(row)),
        "evidence_ids": list(_ids(row)),
        "row": dict(row),
    }


def _matches_quantitative_reduction(
    target: dict[str, Any],
    quantitative: dict[str, Any],
) -> bool:
    target_year = _number(target.get("목표연도"))
    quantitative_year = _number(quantitative.get("연도"))
    if target_year is None or quantitative_year is None or target_year != quantitative_year:
        return False
    if not _same_number(target.get("목표감축량"), quantitative.get("예상감축량")):
        return False
    if _exact_evidence_overlap(target, quantitative):
        return True
    if not _page_overlap(target, quantitative):
        return False
    level = str(target.get("목표수준") or "").strip()
    return level == "연차경로" or _names_overlap(target, quantitative)


def guard_reduction_targets(
    rows: list[dict[str, Any]],
    *,
    quantitative_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep target rows unless they are empty, visual fragments, or canonical duplicates."""
    quantitative_rows = quantitative_rows or []
    retained: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for row in rows:
        reasons: list[str] = []
        if not any(_has_value(row.get(field)) for field in _TARGET_VALUE_FIELDS):
            reasons.append("no_target_quantity")

        sector = _normalize_text(row.get("부문"))
        is_visual = str(row.get("데이터상태") or "").strip() == "visual_only"
        if is_visual and sector in _TARGET_METRIC_LABELS:
            reasons.append("visual_metric_label_as_sector")

        level = str(row.get("목표수준") or "").strip()
        if level in _DETAILED_TARGET_LEVELS and any(
            _matches_quantitative_reduction(row, candidate)
            for candidate in quantitative_rows
            if isinstance(candidate, dict)
        ):
            reasons.append("canonical_quantitative_duplicate")

        if reasons:
            records.append(_record("reduction_targets", row, *reasons))
        else:
            retained.append(row)
    return retained, records


def _foundation_risk_signal_count(row: dict[str, Any]) -> int:
    return sum(_has_value(row.get(field)) for field in _FOUNDATION_RISK_FIELDS)


def _foundation_has_risk_contract(row: dict[str, Any]) -> bool:
    count = _foundation_risk_signal_count(row)
    has_value = _has_value(row.get("값")) or _has_value(row.get("단위"))
    has_identity = any(
        _has_value(row.get(field))
        for field in ("평가유형", "리스크항목", "취약성지표", "기후변수")
    )
    return count >= 2 or (has_identity and has_value)


def _foundation_strength(row: dict[str, Any]) -> int:
    return sum(
        _has_value(row.get(field))
        for field in (
            "과제ID",
            *_FOUNDATION_POLICY_DETAIL_FIELDS,
            *_FOUNDATION_RISK_FIELDS,
            "값",
            "단위",
        )
    )


def _foundation_identity(row: dict[str, Any]) -> tuple[str, str]:
    return (
        _normalize_text(row.get("대응기반영역")),
        _normalize_text(row.get("과제명")),
    )


def _is_heading_echo(
    row: dict[str, Any],
    strongest_by_identity: dict[tuple[str, str], int],
) -> bool:
    name = _normalize_text(row.get("과제명"))
    content = _normalize_text(row.get("주요내용"))
    if not name or name != content or _has_value(row.get("과제ID")):
        return False
    if _foundation_has_risk_contract(row):
        return False
    if any(_has_value(row.get(field)) for field in _FOUNDATION_POLICY_DETAIL_FIELDS):
        return False
    identity = _foundation_identity(row)
    return strongest_by_identity.get(identity, 0) > _foundation_strength(row)


def _matches_mitigation_leakage(
    row: dict[str, Any],
    mitigation_rows: list[dict[str, Any]],
) -> bool:
    area = str(row.get("대응기반영역") or "").strip()
    if area in _FOUNDATION_AREAS or _foundation_has_risk_contract(row):
        return False
    if any(_has_value(row.get(field)) for field in _FOUNDATION_POLICY_DETAIL_FIELDS):
        return False
    return any(
        _names_overlap(row, candidate)
        and (_exact_evidence_overlap(row, candidate) or _page_overlap(row, candidate))
        for candidate in mitigation_rows
        if isinstance(candidate, dict)
    )


def _foundation_duplicate_signature(row: dict[str, Any]) -> tuple[Any, ...]:
    fields = (
        "대응기반영역",
        "과제ID",
        "과제명",
        "정책방향",
        "주요내용",
        "대상",
        "주관부서",
        "기간",
        "평가유형",
        "기후변수",
        "시나리오",
        "기준기간",
        "미래기간",
        "공간단위",
        "부문",
        "리스크항목",
        "취약성지표",
        "값",
        "단위",
        "리스크등급",
        "방법론",
        "자료출처",
        "연계적응과제",
    )
    return (*(_normalize_text(row.get(field)) for field in fields), _pages(row))


def guard_foundation_measures(
    rows: list[dict[str, Any]],
    *,
    mitigation_rows: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Apply the policy-task and climate-risk contracts without creating 12a."""
    mitigation_rows = mitigation_rows or []
    strongest_by_identity: dict[tuple[str, str], int] = defaultdict(int)
    for row in rows:
        identity = _foundation_identity(row)
        if all(identity):
            strongest_by_identity[identity] = max(
                strongest_by_identity[identity],
                _foundation_strength(row),
            )

    retained: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()
    for row in rows:
        reasons: list[str] = []
        has_task_identity = _has_value(row.get("과제ID")) or _has_value(row.get("과제명"))
        has_risk_contract = _foundation_has_risk_contract(row)
        if not has_task_identity and not has_risk_contract:
            reasons.append("missing_task_or_risk_identity")
        elif _is_heading_echo(row, strongest_by_identity):
            reasons.append("repeated_heading_echo")
        elif _matches_mitigation_leakage(row, mitigation_rows):
            reasons.append("mitigation_sheet_duplicate_without_foundation_context")

        signature = _foundation_duplicate_signature(row)
        if not reasons and signature in seen:
            reasons.append("same_page_semantic_duplicate")

        if reasons:
            records.append(_record("foundation_measures", row, *reasons))
        else:
            retained.append(row)
            seen.add(signature)
    return retained, records


def apply_semantic_contract_guards(
    cleaned: dict[str, Any],
) -> list[dict[str, Any]]:
    """Mutate the two public sheets and return a non-public quarantine ledger."""
    records: list[dict[str, Any]] = []
    targets, target_records = guard_reduction_targets(
        list(cleaned.get("reduction_targets", [])),
        quantitative_rows=list(cleaned.get("quantitative_reductions", [])),
    )
    foundation, foundation_records = guard_foundation_measures(
        list(cleaned.get("foundation_measures", [])),
        mitigation_rows=list(cleaned.get("mitigation_projects", [])),
    )
    cleaned["reduction_targets"] = targets
    cleaned["foundation_measures"] = foundation
    records.extend(target_records)
    records.extend(foundation_records)
    return records

