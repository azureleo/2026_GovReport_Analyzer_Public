"""Persist and replay visual-merge inputs without another LLM call."""

from __future__ import annotations

import gzip
import json
from collections import Counter
from copy import deepcopy
from pathlib import Path
from typing import Any

import config


SNAPSHOT_VERSION = 1


def _compact_document_object(row: dict[str, Any]) -> dict[str, Any]:
    return {
        key: deepcopy(row.get(key))
        for key in (
            "object_id", "object_type", "page_number", "sequence", "caption",
            "number", "section", "bbox", "rows", "metadata",
        )
        if key in row
    }


def snapshot_payload(raw_data: dict[str, Any]) -> dict[str, Any]:
    data: dict[str, Any] = {
        "municipality_name": raw_data.get("municipality_name", "알 수 없음"),
    }
    for sheet_key in getattr(config, "EXTRACTION_SHEETS", []):
        rows = raw_data.get(sheet_key)
        if isinstance(rows, list):
            data[sheet_key] = deepcopy([row for row in rows if isinstance(row, dict)])
    observations = raw_data.get("chart_observations")
    data["chart_observations"] = deepcopy(
        [row for row in observations if isinstance(row, dict)]
        if isinstance(observations, list) else []
    )
    objects = raw_data.get("document_objects")
    data["document_objects"] = [
        _compact_document_object(row)
        for row in objects if isinstance(row, dict)
    ] if isinstance(objects, list) else []
    triage = raw_data.get("object_triage")
    data["object_triage"] = deepcopy(
        [row for row in triage if isinstance(row, dict)]
        if isinstance(triage, list) else []
    )
    return {
        "snapshot_version": SNAPSHOT_VERSION,
        "pipeline_version": getattr(config, "PIPELINE_VERSION", ""),
        "data": data,
    }


def write_visual_merge_snapshot(path: str | Path, raw_data: dict[str, Any]) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = snapshot_payload(raw_data)
    with gzip.open(target, "wt", encoding="utf-8") as stream:
        json.dump(
            payload,
            stream,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
    return target


def load_visual_merge_snapshot(path: str | Path) -> dict[str, Any]:
    source = Path(path)
    opener = gzip.open if source.suffix.casefold() == ".gz" else open
    with opener(source, "rt", encoding="utf-8") as stream:
        payload = json.load(stream)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), dict):
        raise ValueError("시각 병합 스냅샷 형식이 아닙니다")
    return payload


def _reset_legacy_observations(rows: list[dict[str, Any]]) -> None:
    for row in rows:
        row["반영여부"] = "검토"
        row["병합상태"] = "candidate"
        row["병합차단사유"] = ""
        row["근거매칭상태"] = "legacy"
        row["근거객체ID"] = ""


def _visual_rows(cleaned: dict[str, Any]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for sheet_key in getattr(config, "EXTRACTION_SHEETS", []):
        for row in cleaned.get(sheet_key, []):
            if not isinstance(row, dict) or str(row.get("데이터상태") or "") != "visual_only":
                continue
            rows.append({"_sheet": sheet_key, **deepcopy(row)})
    return rows


_DUPLICATE_IGNORED_FIELDS = {
    "_sheet", "출처페이지", "근거ID", "데이터상태", "derivation_type",
}


def _duplicate_output_rows(rows: list[dict[str, Any]]) -> int:
    """출처 메타데이터만 다른 동일 시트·동일 값 행의 초과 개수를 센다."""
    seen: set[str] = set()
    duplicates = 0
    for row in rows:
        comparable = {
            key: value
            for key, value in row.items()
            if key not in _DUPLICATE_IGNORED_FIELDS
        }
        key = json.dumps(
            {"_sheet": row.get("_sheet"), **comparable},
            ensure_ascii=False,
            sort_keys=True,
            default=str,
        )
        if key in seen:
            duplicates += 1
        else:
            seen.add(key)
    return duplicates


def _organize_merge_policy(snapshot: dict[str, Any], mode: str) -> dict[str, Any]:
    if mode not in {"legacy", "evidence"}:
        raise ValueError(f"지원하지 않는 병합 모드: {mode}")
    from agents.organizer_agent import OrganizerAgent

    raw = deepcopy(snapshot["data"])
    evaluation_objects = deepcopy(raw.get("document_objects", []))
    evaluation_triage = deepcopy(raw.get("object_triage", []))
    observations = raw.get("chart_observations", [])
    if mode == "legacy":
        raw.pop("document_objects", None)
        raw.pop("object_triage", None)
        _reset_legacy_observations(observations)

    previous_labeled = getattr(config, "VISUAL_MERGE_LABELED_ENABLED", True)
    previous_evidence = getattr(config, "VISUAL_EVIDENCE_MERGE_ENABLED", True)
    try:
        config.VISUAL_MERGE_LABELED_ENABLED = True
        config.VISUAL_EVIDENCE_MERGE_ENABLED = mode == "evidence"
        cleaned = OrganizerAgent().organize(raw)
    finally:
        config.VISUAL_MERGE_LABELED_ENABLED = previous_labeled
        config.VISUAL_EVIDENCE_MERGE_ENABLED = previous_evidence
    # 병합 정책만 A/B 변수로 두고 평가용 원문 객체 분모는 양쪽에 동일하게 유지한다.
    cleaned["document_objects"] = evaluation_objects
    cleaned["object_triage"] = evaluation_triage
    return cleaned


def _merge_policy_result(cleaned: dict[str, Any], mode: str) -> dict[str, Any]:

    candidate_rows = [
        deepcopy(row) for row in cleaned.get("chart_observations", [])
        if isinstance(row, dict)
    ]
    statuses = Counter(str(row.get("병합상태") or "needs_review") for row in candidate_rows)
    matching = Counter(str(row.get("근거매칭상태") or "") for row in candidate_rows)
    review_rows = [row for row in candidate_rows if row.get("병합상태") == "needs_review"]
    output_rows = _visual_rows(cleaned)
    regional_candidates = [
        row for row in candidate_rows
        if str(row.get("대상시트") or "") == "regional_conditions"
    ]
    regional_statuses = Counter(
        str(row.get("병합상태") or "needs_review")
        for row in regional_candidates
    )
    resolution_methods = Counter(
        str(row.get("근거분리방식") or "") for row in candidate_rows
        if str(row.get("근거분리방식") or "")
    )
    composition_statuses = Counter(
        str(row.get("필드조합상태") or "") for row in candidate_rows
        if str(row.get("필드조합상태") or "")
    )
    composition_fills: Counter[str] = Counter()
    composition_conflicts = 0
    for row in candidate_rows:
        fill_names = row.get("필드조합목록") or []
        if isinstance(fill_names, str):
            fill_names = [value.strip() for value in fill_names.split(",") if value.strip()]
        composition_fills.update(str(value) for value in fill_names if value)
        conflicts = row.get("필드조합충돌") or {}
        if isinstance(conflicts, dict) and conflicts:
            composition_conflicts += 1
    regional_output_rows = [
        row for row in output_rows
        if row.get("_sheet") == "regional_conditions"
    ]
    return {
        "mode": mode,
        "metrics": {
            "candidate_count": len(candidate_rows),
            "visual_output_rows": len(output_rows),
            "visual_output_duplicate_rows": _duplicate_output_rows(output_rows),
            "decision_counts": dict(sorted(statuses.items())),
            "evidence_match_counts": dict(sorted(matching.items())),
            "evidence_resolution_method_counts": dict(sorted(resolution_methods.items())),
            "evidence_corrected_count": sum(
                bool(row.get("근거교정여부")) for row in candidate_rows
            ),
            "field_composition_status_counts": dict(sorted(composition_statuses.items())),
            "field_composition_fill_counts": dict(sorted(composition_fills.items())),
            "field_composition_candidate_count": sum(composition_statuses.values()),
            "field_composition_conflict_count": composition_conflicts,
            "g3_missing_key_blocker_count": sum(
                "G3 1차 키 누락(" in str(row.get("병합차단사유") or "")
                for row in candidate_rows
            ),
            "regional_candidate_count": len(regional_candidates),
            "regional_visual_output_rows": len(regional_output_rows),
            "regional_visual_output_duplicate_rows": _duplicate_output_rows(
                regional_output_rows
            ),
            "regional_decision_counts": dict(sorted(regional_statuses.items())),
            "regional_review_reject_candidates": (
                regional_statuses.get("needs_review", 0)
                + regional_statuses.get("reject", 0)
            ),
            "needs_review_reason_coverage": (
                round(sum(bool(row.get("병합차단사유")) for row in review_rows) / len(review_rows), 4)
                if review_rows else None
            ),
            # 스냅샷 재생은 저장 판독값만 정제하며 모델 공급자를 호출하지 않는다.
            "llm_calls": 0,
        },
        "rows": output_rows,
        "candidates": candidate_rows,
    }


def run_merge_policy(snapshot: dict[str, Any], mode: str) -> dict[str, Any]:
    return _merge_policy_result(_organize_merge_policy(snapshot, mode), mode)


def compare_merge_policies_with_data(
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """정책 지표와 평가용 전체 정제 데이터셋을 함께 반환한다."""
    legacy_data = _organize_merge_policy(snapshot, "legacy")
    evidence_data = _organize_merge_policy(snapshot, "evidence")
    legacy = _merge_policy_result(legacy_data, "legacy")
    evidence = _merge_policy_result(evidence_data, "evidence")
    legacy_rows = legacy["metrics"]["visual_output_rows"]
    evidence_rows = evidence["metrics"]["visual_output_rows"]
    return ({
        "snapshot_version": snapshot.get("snapshot_version"),
        "legacy": legacy,
        "evidence": evidence,
        "comparison": {
            "visual_output_row_delta": evidence_rows - legacy_rows,
            "legacy_visual_output_rows": legacy_rows,
            "evidence_visual_output_rows": evidence_rows,
        },
    }, {
        "legacy": legacy_data,
        "evidence": evidence_data,
    })


def compare_merge_policies(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload, _ = compare_merge_policies_with_data(snapshot)
    return payload


def _organize_regional_enrichment_policy(
    snapshot: dict[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    previous = getattr(config, "REGIONAL_VISUAL_ENRICHMENT_ENABLED", True)
    try:
        config.REGIONAL_VISUAL_ENRICHMENT_ENABLED = enabled
        return _organize_merge_policy(snapshot, "evidence")
    finally:
        config.REGIONAL_VISUAL_ENRICHMENT_ENABLED = previous


def compare_regional_enrichment_with_data(
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """동일 근거 병합에서 02 시각 계약 보완만 OFF/ON으로 비교한다."""
    before_data = _organize_regional_enrichment_policy(snapshot, False)
    after_data = _organize_regional_enrichment_policy(snapshot, True)
    before = _merge_policy_result(before_data, "regional_enrichment_off")
    after = _merge_policy_result(after_data, "regional_enrichment_on")
    before_metrics = before["metrics"]
    after_metrics = after["metrics"]
    return ({
        "snapshot_version": snapshot.get("snapshot_version"),
        "before": before,
        "after": after,
        "comparison": {
            "regional_visual_output_row_delta": (
                after_metrics["regional_visual_output_rows"]
                - before_metrics["regional_visual_output_rows"]
            ),
            "regional_review_reject_delta": (
                after_metrics["regional_review_reject_candidates"]
                - before_metrics["regional_review_reject_candidates"]
            ),
            "regional_duplicate_output_row_delta": (
                after_metrics["regional_visual_output_duplicate_rows"]
                - before_metrics["regional_visual_output_duplicate_rows"]
            ),
            "llm_call_delta": 0,
        },
    }, {
        "before": before_data,
        "after": after_data,
    })


def compare_regional_enrichment(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload, _ = compare_regional_enrichment_with_data(snapshot)
    return payload


def _organize_g4_reference_policy(
    snapshot: dict[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    previous = getattr(config, "VISUAL_REFERENCE_GATE_REFINEMENT_ENABLED", True)
    try:
        config.VISUAL_REFERENCE_GATE_REFINEMENT_ENABLED = enabled
        return _organize_merge_policy(snapshot, "evidence")
    finally:
        config.VISUAL_REFERENCE_GATE_REFINEMENT_ENABLED = previous


def _g4_reference_blocker_count(result: dict[str, Any], sheet_key: str = "") -> int:
    return sum(
        (not sheet_key or str(row.get("대상시트") or "") == sheet_key)
        and "G4 참고자료/사례 판정" in str(row.get("병합차단사유") or "")
        for row in result.get("candidates", [])
    )


def compare_g4_reference_gate_with_data(
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """동일 판독값에서 G4 참고자료 재판정 범위만 OFF/ON으로 비교한다."""
    before_data = _organize_g4_reference_policy(snapshot, False)
    after_data = _organize_g4_reference_policy(snapshot, True)
    before = _merge_policy_result(before_data, "g4_reference_refinement_off")
    after = _merge_policy_result(after_data, "g4_reference_refinement_on")
    before_g4 = _g4_reference_blocker_count(before)
    after_g4 = _g4_reference_blocker_count(after)
    before_regional_g4 = _g4_reference_blocker_count(before, "regional_conditions")
    after_regional_g4 = _g4_reference_blocker_count(after, "regional_conditions")
    return ({
        "snapshot_version": snapshot.get("snapshot_version"),
        "before": before,
        "after": after,
        "comparison": {
            "g4_reference_blocker_delta": after_g4 - before_g4,
            "regional_g4_reference_blocker_delta": (
                after_regional_g4 - before_regional_g4
            ),
            "visual_output_row_delta": (
                after["metrics"]["visual_output_rows"]
                - before["metrics"]["visual_output_rows"]
            ),
            "regional_visual_output_row_delta": (
                after["metrics"]["regional_visual_output_rows"]
                - before["metrics"]["regional_visual_output_rows"]
            ),
            "visual_output_duplicate_row_delta": (
                after["metrics"]["visual_output_duplicate_rows"]
                - before["metrics"]["visual_output_duplicate_rows"]
            ),
            "llm_call_delta": 0,
        },
        "g4_reference_blockers": {
            "before": before_g4,
            "after": after_g4,
            "regional_before": before_regional_g4,
            "regional_after": after_regional_g4,
        },
    }, {
        "before": before_data,
        "after": after_data,
    })


def compare_g4_reference_gate(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload, _ = compare_g4_reference_gate_with_data(snapshot)
    return payload


def _organize_exact_object_resolution_policy(
    snapshot: dict[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    previous = getattr(config, "VISUAL_EXACT_OBJECT_RESOLUTION_ENABLED", True)
    try:
        config.VISUAL_EXACT_OBJECT_RESOLUTION_ENABLED = enabled
        return _organize_merge_policy(snapshot, "evidence")
    finally:
        config.VISUAL_EXACT_OBJECT_RESOLUTION_ENABLED = previous


def compare_exact_object_resolution_with_data(
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """동일 판독값에서 단일 원문 객체 분리 계약만 OFF/ON으로 비교한다."""
    before_data = _organize_exact_object_resolution_policy(snapshot, False)
    after_data = _organize_exact_object_resolution_policy(snapshot, True)
    before = _merge_policy_result(before_data, "exact_object_resolution_off")
    after = _merge_policy_result(after_data, "exact_object_resolution_on")
    before_metrics = before["metrics"]
    after_metrics = after["metrics"]

    def match_count(result: dict[str, Any], status: str) -> int:
        return int(result["metrics"]["evidence_match_counts"].get(status, 0))

    return ({
        "snapshot_version": snapshot.get("snapshot_version"),
        "before": before,
        "after": after,
        "comparison": {
            "exact_match_delta": match_count(after, "exact") - match_count(before, "exact"),
            "mixed_object_delta": (
                match_count(after, "mixed_objects")
                - match_count(before, "mixed_objects")
            ),
            "corrected_evidence_delta": (
                after_metrics["evidence_corrected_count"]
                - before_metrics["evidence_corrected_count"]
            ),
            "visual_output_row_delta": (
                after_metrics["visual_output_rows"]
                - before_metrics["visual_output_rows"]
            ),
            "visual_output_duplicate_row_delta": (
                after_metrics["visual_output_duplicate_rows"]
                - before_metrics["visual_output_duplicate_rows"]
            ),
            "llm_call_delta": 0,
        },
    }, {
        "before": before_data,
        "after": after_data,
    })


def compare_exact_object_resolution(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload, _ = compare_exact_object_resolution_with_data(snapshot)
    return payload


def _organize_visual_field_composition_policy(
    snapshot: dict[str, Any],
    enabled: bool,
) -> dict[str, Any]:
    previous = getattr(config, "VISUAL_FIELD_COMPOSITION_ENABLED", True)
    try:
        config.VISUAL_FIELD_COMPOSITION_ENABLED = enabled
        return _organize_merge_policy(snapshot, "evidence")
    finally:
        config.VISUAL_FIELD_COMPOSITION_ENABLED = previous


def compare_visual_field_composition_with_data(
    snapshot: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, dict[str, Any]]]:
    """동일 판독값에서 캡션·축·범례 필드 조합만 OFF/ON으로 비교한다."""
    before_data = _organize_visual_field_composition_policy(snapshot, False)
    after_data = _organize_visual_field_composition_policy(snapshot, True)
    before = _merge_policy_result(before_data, "visual_field_composition_off")
    after = _merge_policy_result(after_data, "visual_field_composition_on")
    before_metrics = before["metrics"]
    after_metrics = after["metrics"]
    return ({
        "snapshot_version": snapshot.get("snapshot_version"),
        "before": before,
        "after": after,
        "comparison": {
            "visual_output_row_delta": (
                after_metrics["visual_output_rows"] - before_metrics["visual_output_rows"]
            ),
            "visual_output_duplicate_row_delta": (
                after_metrics["visual_output_duplicate_rows"]
                - before_metrics["visual_output_duplicate_rows"]
            ),
            "g3_missing_key_blocker_delta": (
                after_metrics["g3_missing_key_blocker_count"]
                - before_metrics["g3_missing_key_blocker_count"]
            ),
            "field_composition_candidate_delta": (
                after_metrics["field_composition_candidate_count"]
                - before_metrics["field_composition_candidate_count"]
            ),
            "field_composition_conflict_delta": (
                after_metrics["field_composition_conflict_count"]
                - before_metrics["field_composition_conflict_count"]
            ),
            "llm_call_delta": 0,
        },
    }, {
        "before": before_data,
        "after": after_data,
    })


def compare_visual_field_composition(snapshot: dict[str, Any]) -> dict[str, Any]:
    payload, _ = compare_visual_field_composition_with_data(snapshot)
    return payload
