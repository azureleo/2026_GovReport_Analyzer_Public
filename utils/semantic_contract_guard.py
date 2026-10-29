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


# ──────────────────────────────────────────────────────────────────────
# v8-1 S2-4: 03·04·05·09 계약 가드. 다른 정식 시트가 같은 의미를 갖거나 시트
# 최소 계약을 못 채운 행만 격리한다. 값이 다른 같은 키 행은 격리하지 않는다.
# ──────────────────────────────────────────────────────────────────────
_EMISSION_SECTOR_FIELD = {
    "emissions_regional": "부문",
    "emissions_management": "관리부문",
    "emissions_forecast": "부문",
}
_EMISSION_VALUE_FIELD = {
    "emissions_regional": "배출량",
    "emissions_management": "배출량",
    "emissions_forecast": "전망값",
}
# 부문 자리에 지표 라벨이 들어온 행(06 가드의 visual_metric_label_as_sector와 같은 판정).
# 골든 05가 '직접배출량'·'간접배출량'을 부문으로 쓰므로 직접/간접 접두 라벨은 제외한다.
_SECTOR_METRIC_LABELS = {
    _normalize_text(label)
    for label in ("총배출량", "순배출량", "배출량", "감축량", "흡수량", "전망", "배출전망", "bau")
}
_SECTOR_LABEL_KEEP_PREFIXES = ("직접", "간접")
_DEFAULT_YEAR_LOWER = 1990
_DEFAULT_YEAR_UPPER = 2060
_ANNUAL_PLAN_CONTENT_FIELDS = (
    "연간계획", "목표물량", "규제혁신계획", "입법계획", "기간시작", "기간종료",
)
_NEAR_DUPLICATE_MIN_CHARS = 8


def document_year_bounds(document_meta_rows: list[dict[str, Any]] | None) -> tuple[int, int]:
    """문서 메타의 계획종료연도가 있으면 상한을 종료연도+20으로 넓힌다(기본 1990~2060)."""
    upper = _DEFAULT_YEAR_UPPER
    for row in document_meta_rows or []:
        if not isinstance(row, dict):
            continue
        end_year = _number(row.get("계획종료연도"))
        if end_year is not None:
            upper = max(upper, int(end_year) + 20)
    return _DEFAULT_YEAR_LOWER, upper


def _is_metric_label_sector(value: Any) -> bool:
    text = _normalize_text(value)
    if not text or text.startswith(_SECTOR_LABEL_KEEP_PREFIXES):
        return False
    return text in _SECTOR_METRIC_LABELS


def _year_out_of_bounds(value: Any, bounds: tuple[int, int]) -> bool:
    year = _number(value)
    if year is None:
        return False
    return year < bounds[0] or year > bounds[1]


def guard_emission_rows(
    rows: list[dict[str, Any]],
    sheet_key: str,
    *,
    year_bounds: tuple[int, int] = (_DEFAULT_YEAR_LOWER, _DEFAULT_YEAR_UPPER),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """03·04·05 공통: 키 계약 미달, 지표 라벨 부문, 문서 범위 밖 연도만 격리한다."""
    sector_field = _EMISSION_SECTOR_FIELD[sheet_key]
    value_field = _EMISSION_VALUE_FIELD[sheet_key]
    retained: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    for row in rows:
        reasons: list[str] = []
        if (
            not _has_value(row.get(sector_field))
            or _number(row.get("연도")) is None
            or _number(row.get(value_field)) is None
        ):
            reasons.append("missing_key_contract")
        # 골든 03/04는 '총배출량(연료공급량기준)' 같은 합계 라벨 행을 정식 행으로 두므로
        # 지표 라벨 판정은 06 가드와 같이 시각 유래 행에만 적용한다.
        is_visual = str(row.get("데이터상태") or "").strip() == "visual_only"
        if is_visual and (
            _is_metric_label_sector(row.get(sector_field)) or _is_metric_label_sector(row.get("세부부문"))
        ):
            reasons.append("metric_label_as_sector")
        if _year_out_of_bounds(row.get("연도"), year_bounds):
            reasons.append("year_out_of_document_range")
        if reasons:
            records.append(_record(sheet_key, row, *reasons))
        else:
            retained.append(row)
    return retained, records


def _annual_identity(row: dict[str, Any]) -> tuple[str, str]:
    project_id = _normalize_text(row.get("관리번호"))
    if project_id:
        return ("id", project_id)
    return ("name", _normalize_text(row.get("사업명")))


def guard_annual_implementation(
    rows: list[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """09: 식별자 없음·계획 내용 없음·같은 (식별자, 연도)의 포함 관계 중복문만 격리한다."""
    retained: list[dict[str, Any]] = []
    records: list[dict[str, Any]] = []
    candidates: list[dict[str, Any]] = []
    for row in rows:
        reasons: list[str] = []
        if not _has_value(row.get("관리번호")) and not _has_value(row.get("사업명")):
            reasons.append("missing_identity")
        if not any(_has_value(row.get(field)) for field in _ANNUAL_PLAN_CONTENT_FIELDS):
            reasons.append("no_plan_content")
        if reasons:
            records.append(_record("annual_implementation", row, *reasons))
        else:
            candidates.append(row)

    groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
    for row in candidates:
        identity = _annual_identity(row)
        if not identity[1]:
            retained.append(row)
            continue
        groups[(*identity, _number(row.get("연도")))].append(row)

    for members in groups.values():
        dropped: set[int] = set()
        for index, row in enumerate(members):
            if index in dropped:
                continue
            row_text = _normalize_text(row.get("연간계획"))
            for other_index, other in enumerate(members):
                if other_index == index or other_index in dropped:
                    continue
                if _number(row.get("목표물량")) is not None or _number(other.get("목표물량")) is not None:
                    if not _same_number(row.get("목표물량"), other.get("목표물량")):
                        continue
                other_text = _normalize_text(other.get("연간계획"))
                shorter, longer = sorted((row_text, other_text), key=len)
                if len(shorter) < _NEAR_DUPLICATE_MIN_CHARS or shorter not in longer:
                    continue
                # 짧은 쪽을 격리한다(정보 손실 없음). 길이가 같으면 뒤의 행을 격리.
                loser = other_index if len(other_text) <= len(row_text) else index
                dropped.add(loser)
                records.append(_record(
                    "annual_implementation", members[loser], "near_duplicate_plan_text",
                ))
                if loser == index:
                    break
        retained.extend(row for index, row in enumerate(members) if index not in dropped)
    return retained, records


def summarize_quarantine(records: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    summary: dict[str, dict[str, int]] = {}
    for record in records:
        sheet = str(record.get("sheet_key") or "")
        by_reason = summary.setdefault(sheet, {})
        for reason in record.get("reason_codes", []) or ["unspecified"]:
            by_reason[str(reason)] = by_reason.get(str(reason), 0) + 1
    return summary


def write_quarantine_ledger(records: list[dict[str, Any]], output_path: Any) -> Any:
    """격리 원장을 산출물 옆 `<stem>_격리원장.json`으로 쓴다(엑셀 시트는 추가하지 않는다)."""
    from pathlib import Path

    target = Path(output_path)
    ledger_path = target.with_name(f"{target.stem}_격리원장.json")
    payload = {
        "summary": summarize_quarantine(records),
        "records": records,
    }
    ledger_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8",
    )
    return ledger_path


def apply_semantic_contract_guards(
    cleaned: dict[str, Any],
    *,
    contract_drops: dict[str, list[dict[str, Any]]] | None = None,
) -> list[dict[str, Any]]:
    """Mutate the guarded sheets and return a non-public quarantine ledger.

    contract_drops: 정제기가 키 계약 미달로 이미 제외한 행(시트키 → 행 목록). 격리
    원장에 missing_key_contract로 함께 남긴다(삭제 자체는 정제기가 담당).
    """
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

    year_bounds = document_year_bounds(cleaned.get("document_meta"))
    for sheet_key in ("emissions_regional", "emissions_management", "emissions_forecast"):
        rows = cleaned.get(sheet_key)
        if not isinstance(rows, list):
            continue
        kept, sheet_records = guard_emission_rows(rows, sheet_key, year_bounds=year_bounds)
        cleaned[sheet_key] = kept
        records.extend(sheet_records)
    annual = cleaned.get("annual_implementation")
    if isinstance(annual, list):
        kept, annual_records = guard_annual_implementation(annual)
        cleaned["annual_implementation"] = kept
        records.extend(annual_records)

    for sheet_key, dropped_rows in (contract_drops or {}).items():
        for row in dropped_rows:
            if isinstance(row, dict):
                records.append(_record(sheet_key, row, "missing_key_contract"))
    return records
